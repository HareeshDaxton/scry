"""Liveness signals: is the worker alive, and is it doing anything.

Those are two different questions and they fail in two different ways, so this
module answers both separately.

The heartbeat runs on its own thread, which means it keeps beating while the
agent's main thread works — that is the point, since a heartbeat that stopped
whenever the agent was busy would be useless. But it also means a *stalled*
agent, wedged in a loop it will never leave, heartbeats perfectly. Heartbeat
alone therefore cannot detect a stall, and section 1.12's ``agent_stalled`` rule
would have nothing to key on.

Hence two clocks per agent: ``last_heartbeat`` says the process is alive,
``last_progress`` says it is making progress. A crash stops the first; a stall
stops only the second.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from scry.runtime.bus import Publisher

# Both defaults mirror config.agents.*; the pool passes the configured values.
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 10.0
DEFAULT_HEARTBEAT_TIMEOUT_SECONDS = 30.0
DEFAULT_STALL_TIMEOUT_SECONDS = 300.0


class Heartbeat:
    """A daemon thread that publishes a heartbeat at a fixed interval."""

    def __init__(
        self,
        publisher: Publisher,
        *,
        interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self._publisher = publisher
        self._interval = max(0.01, float(interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sequence = 0

    @property
    def sequence(self) -> int:
        return self._sequence

    def start(self) -> None:
        if self._thread is not None:
            return
        # Beat once immediately. The monitor otherwise has no observation at all
        # for a whole interval after spawn, and a slow-starting agent would look
        # dead during exactly the window in which it is most likely to fail.
        self._beat()
        # Daemon, so a wedged heartbeat can never be the reason a worker refuses
        # to exit.
        self._thread = threading.Thread(target=self._loop, name="scry-heartbeat", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 2.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)

    def _loop(self) -> None:
        # Event.wait, never time.sleep: wait returns the instant stop() is
        # called, where sleep would make every shutdown pay a full interval.
        while not self._stop.wait(self._interval):
            self._beat()

    def _beat(self) -> None:
        self._sequence += 1
        try:
            self._publisher.heartbeat(self._sequence)
        except Exception:
            # The parent is gone or the queue is closed. There is nobody left to
            # tell, and raising on a daemon thread would print a traceback that
            # looks like a crash during an ordinary shutdown.
            self._stop.set()


@dataclass
class AgentLiveness:
    """What the parent has observed about one agent."""

    name: str
    last_heartbeat: float
    last_progress: float
    heartbeats: int = 0
    progress_fraction: float | None = None
    task: str | None = None


@dataclass
class HeartbeatMonitor:
    """Parent-side record of which agents are alive and which are moving.

    Times are :func:`time.monotonic`, never wall clock. An NTP correction or a
    daylight-saving step would otherwise move every observation at once and
    declare the entire pool dead in the same instant — a failure that is both
    spectacular and completely inexplicable from the logs.

    ``clock`` is injectable so tests can assert a thirty-second timeout without
    waiting thirty seconds.
    """

    heartbeat_timeout: float = DEFAULT_HEARTBEAT_TIMEOUT_SECONDS
    stall_timeout: float = DEFAULT_STALL_TIMEOUT_SECONDS
    clock: Callable[[], float] = time.monotonic
    _agents: dict[str, AgentLiveness] = field(default_factory=dict)

    def register(self, name: str) -> None:
        now = self.clock()
        self._agents[name] = AgentLiveness(name=name, last_heartbeat=now, last_progress=now)

    def forget(self, name: str) -> None:
        self._agents.pop(name, None)

    def record_heartbeat(self, name: str) -> None:
        agent = self._agents.get(name)
        if agent is None:
            self.register(name)
            agent = self._agents[name]
        agent.last_heartbeat = self.clock()
        agent.heartbeats += 1

    def record_progress(self, name: str, fraction: float, task: str | None = None) -> None:
        agent = self._agents.get(name)
        if agent is None:
            self.register(name)
            agent = self._agents[name]
        now = self.clock()
        # Progress is also proof of life, so it refreshes both clocks. An agent
        # reporting progress while its heartbeat thread has died is working fine.
        agent.last_progress = now
        agent.last_heartbeat = now
        agent.progress_fraction = fraction
        agent.task = task

    def liveness(self, name: str) -> AgentLiveness | None:
        return self._agents.get(name)

    def silent(self) -> tuple[str, ...]:
        """Agents whose heartbeat has stopped: the process is dead or wedged."""
        now = self.clock()
        return tuple(
            name
            for name, agent in self._agents.items()
            if now - agent.last_heartbeat > self.heartbeat_timeout
        )

    def stalled(self) -> tuple[str, ...]:
        """Agents still beating but making no progress.

        The signal section 1.12's ``agent_stalled`` rule keys on. Excludes agents
        that are already silent, because those have a different problem and a
        different remedy.
        """
        now = self.clock()
        silent = set(self.silent())
        return tuple(
            name
            for name, agent in self._agents.items()
            if name not in silent and now - agent.last_progress > self.stall_timeout
        )

    def snapshot(self) -> dict[str, AgentLiveness]:
        return dict(self._agents)
