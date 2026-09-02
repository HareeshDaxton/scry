"""Spawning, supervising and shutting down worker processes.

The pool provides *mechanism*: it starts agents, tracks whether they are alive,
records that one died and why, and stops them all cleanly. It provides no
*policy*: nothing here decides whether a crashed agent should be restarted, how
many times, or what to run next. That is the Conductor's rule table in section
1.12, and keeping the two apart is what makes the supervision rules testable
without spawning anything.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from scry.runtime.agent import (
    EXIT_INTERRUPTED,
    EXIT_OK,
    AgentProcess,
    AgentRuntimeSpec,
    run_agent,
)
from scry.runtime.bus import MessageBus
from scry.runtime.heartbeat import HeartbeatMonitor
from scry.runtime.messages import AgentStatus, ControlCommand, Message, MessageType
from scry.util.errors import AgentError
from scry.util.logging import LogRelay, start_log_listener

if TYPE_CHECKING:  # pragma: no cover
    from scry.config import Config

log = logging.getLogger(__name__)

# How long a cooperative stop is given before escalating.
DEFAULT_SHUTDOWN_GRACE_SECONDS = 5.0
TERMINATE_GRACE_SECONDS = 2.0
KILL_GRACE_SECONDS = 1.0


@dataclass
class ManagedAgent:
    """The parent's record of one spawned agent."""

    name: str
    process: Any
    control: Any
    stop_event: Any
    started_at: float
    status: AgentStatus = AgentStatus.IDLE
    crash_count: int = 0
    exit_code: int | None = None
    interrupted: bool = False
    last_error: str | None = None
    last_traceback: str | None = None
    claims_appended: int = 0
    last_claim_seq: int = 0

    @property
    def alive(self) -> bool:
        return bool(self.process.is_alive())


class AgentPool:
    """A fixed-capacity set of worker processes and the bus they report on."""

    def __init__(
        self,
        config: Config,
        *,
        bus: MessageBus | None = None,
        log_relay: LogRelay | None = None,
        database: Path | None = None,
        target_path: Path | None = None,
        max_concurrent: int | None = None,
    ) -> None:
        self.config = config
        self.bus = bus if bus is not None else MessageBus()

        # Ownership rule: whatever this pool created, this pool stops. A relay
        # handed in belongs to the caller and outlives us.
        self._owns_relay = log_relay is None
        self.log_relay = (
            log_relay if log_relay is not None else start_log_listener(context=self.bus.context)
        )

        self.database = database
        self.target_path = target_path
        self.max_concurrent = (
            max_concurrent if max_concurrent is not None else config.agents.max_concurrent
        )
        self.monitor = HeartbeatMonitor(
            heartbeat_timeout=float(config.agents.heartbeat_timeout_seconds),
            stall_timeout=float(config.agents.timeout_seconds),
        )

        self._agents: dict[str, ManagedAgent] = {}
        self._shutting_down = False
        self._shutdown_complete = False
        self._subscribe()

    # -- bus wiring --------------------------------------------------------
    def _subscribe(self) -> None:
        self.bus.subscribe(str(MessageType.HEARTBEAT), self._on_heartbeat)
        self.bus.subscribe(str(MessageType.PROGRESS), self._on_progress)
        self.bus.subscribe(str(MessageType.STATUS), self._on_status)
        self.bus.subscribe(str(MessageType.ERROR), self._on_error)
        self.bus.subscribe(str(MessageType.CLAIM_BATCH), self._on_claim_batch)

    def _on_heartbeat(self, message: Message) -> None:
        self.monitor.record_heartbeat(message.sender)

    def _on_progress(self, message: Message) -> None:
        self.monitor.record_progress(
            message.sender,
            float(message.payload.get("fraction", 0.0)),
            message.payload.get("task"),
        )

    def _on_status(self, message: Message) -> None:
        managed = self._agents.get(message.sender)
        if managed is not None:
            managed.status = AgentStatus(message.payload["status"])

    def _on_error(self, message: Message) -> None:
        managed = self._agents.get(message.sender)
        if managed is not None:
            managed.last_error = message.payload.get("message")
            managed.last_traceback = message.payload.get("traceback")

    def _on_claim_batch(self, message: Message) -> None:
        managed = self._agents.get(message.sender)
        if managed is not None:
            managed.claims_appended += int(message.payload.get("count", 0))
            managed.last_claim_seq = int(message.payload.get("last_seq", 0))

    # -- lifecycle ---------------------------------------------------------
    def spawn(self, agent: AgentProcess) -> ManagedAgent:
        """Start one agent in its own process.

        Raises rather than queueing when the pool is full. Deciding *which*
        agent runs when a slot frees is scheduling, and scheduling belongs to the
        Conductor; a queue here would be a second scheduler with no rules.
        """
        if agent.name in self._agents:
            raise AgentError("an agent with this name is already in the pool", agent=agent.name)
        if len(self._agents) >= self.max_concurrent:
            raise AgentError(
                f"pool is at its concurrency cap of {self.max_concurrent}; "
                f"nothing can start until an agent finishes",
                agent=agent.name,
            )

        agent.assert_picklable()

        control = self.bus.control_queue()
        stop_event = self.bus.context.Event()
        spec = AgentRuntimeSpec(
            name=agent.name,
            config=self.config,
            inbox=self.bus.inbox,
            control=control,
            stop_event=stop_event,
            log_queue=self.log_relay.queue,
            database=self.database,
            target_path=self.target_path,
            heartbeat_interval=float(self.config.agents.heartbeat_interval_seconds),
        )

        # Not daemonic. A daemon process is killed abruptly at parent exit, which
        # would abandon a half-written claim-log transaction; shutdown() joins
        # every child explicitly instead.
        process = self.bus.context.Process(
            target=run_agent,
            args=(agent, spec),
            name=f"scry-{agent.name}",
            daemon=False,
        )
        process.start()

        managed = ManagedAgent(
            name=agent.name,
            process=process,
            control=control,
            stop_event=stop_event,
            started_at=time.monotonic(),
            status=AgentStatus.RUNNING,
        )
        self._agents[agent.name] = managed
        self.monitor.register(agent.name)
        log.debug("spawned agent %s as pid %s", agent.name, process.pid)
        return managed

    def spawn_all(self, agents: Iterable[AgentProcess]) -> tuple[ManagedAgent, ...]:
        return tuple(self.spawn(agent) for agent in agents)

    # -- supervision -------------------------------------------------------
    def poll(self, *, timeout: float = 0.0) -> int:
        """Dispatch pending messages, then reap anything that exited."""
        handled = self.bus.dispatch(timeout=timeout)
        self.reap()
        return handled

    def reap(self) -> tuple[str, ...]:
        """Record processes that have exited. Returns the newly crashed ones."""
        crashed: list[str] = []
        for managed in self._agents.values():
            if managed.exit_code is not None or managed.alive:
                continue

            code = managed.process.exitcode
            managed.exit_code = code

            if code == EXIT_INTERRUPTED:
                # A user interrupt is not a malfunction. Counting it as a crash
                # would let three Ctrl+Cs trip the Conductor's three-strikes rule
                # and permanently disable an agent that never failed.
                managed.interrupted = True
                managed.status = AgentStatus.COMPLETED
            elif code == EXIT_OK:
                managed.status = AgentStatus.COMPLETED
            elif self._shutting_down:
                # terminate() yields -SIGTERM here and 1 on Windows. Neither is a
                # crash; we asked for it.
                managed.status = AgentStatus.COMPLETED
            else:
                managed.status = AgentStatus.CRASHED
                managed.crash_count += 1
                crashed.append(managed.name)
                log.warning(
                    "agent %s exited with code %s: %s",
                    managed.name,
                    code,
                    managed.last_error or "no error reported",
                )
        return tuple(crashed)

    def send(self, name: str, message: Message) -> None:
        managed = self._require(name)
        self.bus.send(managed.control, message)

    def broadcast(self, message: Message) -> None:
        self.bus.broadcast([m.control for m in self._agents.values()], message)

    def request_stop(self, name: str) -> None:
        managed = self._require(name)
        managed.stop_event.set()
        self.bus.send(managed.control, self.bus.stop_command(recipient=name))

    def kill(self, name: str) -> None:
        """Stop one agent now, escalating from terminate to kill."""
        managed = self._require(name)
        managed.stop_event.set()
        if managed.alive:
            managed.process.terminate()
            managed.process.join(TERMINATE_GRACE_SECONDS)
        if managed.alive:
            managed.process.kill()
            managed.process.join(KILL_GRACE_SECONDS)
        managed.exit_code = managed.process.exitcode
        managed.status = AgentStatus.CRASHED if managed.exit_code else AgentStatus.COMPLETED

    def shutdown(self, *, grace: float = DEFAULT_SHUTDOWN_GRACE_SECONDS) -> None:
        """Stop every agent, then release the bus and the log relay.

        The order is load-bearing throughout: ask nicely, then escalate; drain
        the bus before closing it, or the dying agents' final errors are lost;
        stop the log relay last, after every worker has been joined, or the same
        happens to their last log lines.

        Idempotent. Shutdown paths genuinely run twice — a context manager whose
        body already shut down, a test fixture cleaning up after the test did —
        and the second pass would otherwise put a stop command onto a queue it
        had already closed.
        """
        if self._shutdown_complete:
            return
        self._shutting_down = True

        for managed in self._agents.values():
            managed.stop_event.set()
        self.broadcast(self.bus.stop_command())

        deadline = time.monotonic() + grace
        for managed in self._agents.values():
            managed.process.join(max(0.0, deadline - time.monotonic()))

        for managed in self._agents.values():
            if managed.alive:
                log.warning("agent %s ignored the stop request; terminating", managed.name)
                managed.process.terminate()
                managed.process.join(TERMINATE_GRACE_SECONDS)
        for managed in self._agents.values():
            if managed.alive:
                log.warning("agent %s survived terminate; killing", managed.name)
                managed.process.kill()
                managed.process.join(KILL_GRACE_SECONDS)

        # Dispatch before reaping. Everything is joined, so whatever is still
        # queued is the agents' final word — the crash traceback, the last
        # status. Reaping first would overwrite those statuses with ones
        # inferred from an exit code.
        self.bus.dispatch()
        self.reap()

        for managed in self._agents.values():
            _close_queue(managed.control)
        self.bus.close()

        if self._owns_relay:
            self.log_relay.stop()
        self._shutdown_complete = True

    # -- inspection --------------------------------------------------------
    def _require(self, name: str) -> ManagedAgent:
        managed = self._agents.get(name)
        if managed is None:
            raise AgentError("no such agent in this pool", agent=name)
        return managed

    def __getitem__(self, name: str) -> ManagedAgent:
        return self._require(name)

    def __contains__(self, name: object) -> bool:
        return name in self._agents

    def __len__(self) -> int:
        return len(self._agents)

    @property
    def agents(self) -> tuple[ManagedAgent, ...]:
        return tuple(self._agents.values())

    def running(self) -> tuple[str, ...]:
        return tuple(name for name, m in self._agents.items() if m.alive)

    def statuses(self) -> dict[str, AgentStatus]:
        return {name: m.status for name, m in self._agents.items()}

    def crashed(self) -> tuple[str, ...]:
        return tuple(name for name, m in self._agents.items() if m.status is AgentStatus.CRASHED)

    def __enter__(self) -> AgentPool:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.shutdown()


def _close_queue(q: Any) -> None:
    try:
        q.close()
        q.join_thread()
    except (OSError, ValueError, AttributeError):
        pass


__all__ = [
    "DEFAULT_SHUTDOWN_GRACE_SECONDS",
    "AgentPool",
    "ControlCommand",
    "ManagedAgent",
]
