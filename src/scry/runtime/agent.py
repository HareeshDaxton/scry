"""The base class every analysis agent subclasses, and the child entry point.

An agent is written as one method — :meth:`AgentProcess.execute` — that receives
an :class:`AgentContext` and does work. Everything around it (logging, the
heartbeat, crash capture, cooperative shutdown, the database connection) is
handled here, so that Phase 2's Archivist and everything after it contain
analysis and nothing else.
"""

from __future__ import annotations

import logging
import pickle
import queue as queue_module
import sqlite3
import sys
import traceback
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from scry.runtime.bus import Publisher
from scry.runtime.heartbeat import DEFAULT_HEARTBEAT_INTERVAL_SECONDS, Heartbeat
from scry.runtime.messages import AgentStatus, ControlCommand, Message, MessageType, decode
from scry.storage.claims import Claim, append_claims
from scry.storage.db import connect_writer
from scry.util.errors import AgentError
from scry.util.logging import setup_worker_logging
from scry.util.redact import redact

if TYPE_CHECKING:  # pragma: no cover
    from scry.config import Config

# Exit codes the child uses. Distinct values, because the parent's only
# information about a process that has already gone is its exit code.
EXIT_OK = 0
EXIT_CRASH = 1
# 128 + SIGINT, the shell convention for "interrupted".
EXIT_INTERRUPTED = 130

# Roughly a kilobyte per claim payload, so five thousand pending is about five
# megabytes of unmerged log: negligible against storage.max_memory_mb, and high
# enough that a worker essentially never blocks on it. Section 1.6 built the
# lever and left the policy to whoever owns the pool, which is this section.
DEFAULT_MAX_PENDING_CLAIMS = 5000


@dataclass(frozen=True)
class AgentRuntimeSpec:
    """Everything the child needs, and nothing that cannot cross to it.

    Assembled by the pool in the parent and passed as a ``Process`` argument.
    The queues and the event are picklable only in that position — which is the
    whole reason they are gathered into one object handed over at spawn time
    rather than reached for later.
    """

    name: str
    config: Config
    inbox: Any
    control: Any
    stop_event: Any
    log_queue: Any
    database: Path | None = None
    target_path: Path | None = None
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS


@dataclass
class AgentContext:
    """The handle an agent uses to talk to the world.

    Built inside the child, so unlike everything in
    :class:`AgentRuntimeSpec` it is free to hold live resources — a logger, a
    database connection, a thread.
    """

    name: str
    config: Config
    publisher: Publisher
    logger: logging.Logger
    stop_event: Any
    control: Any
    database: Path | None = None
    target_path: Path | None = None
    max_pending_claims: int = DEFAULT_MAX_PENDING_CLAIMS

    _connection: sqlite3.Connection | None = field(default=None, init=False, repr=False)
    _stop_requested: bool = field(default=False, init=False, repr=False)
    _control_buffer: list[Message] = field(default_factory=list, init=False, repr=False)

    # -- shutdown ----------------------------------------------------------
    def should_stop(self) -> bool:
        """Whether the agent has been asked to wind up.

        Long loops call this between units of work. Cooperative rather than
        signal-driven: ``terminate()`` would abandon a half-written transaction,
        and Windows has no signal semantics worth relying on.
        """
        self._drain_control()
        return self._stop_requested or self.stop_event.is_set()

    def poll_control(self) -> tuple[Message, ...]:
        """Control messages other than stop, since the last call."""
        self._drain_control()
        buffered = tuple(self._control_buffer)
        self._control_buffer.clear()
        return buffered

    def _drain_control(self) -> None:
        while True:
            try:
                raw = self.control.get_nowait()
            except queue_module.Empty:
                return
            except (OSError, ValueError):
                return
            message = decode(raw)
            is_stop = (
                message.type is MessageType.CONTROL
                and message.payload.get("command") == ControlCommand.STOP
            )
            if is_stop:
                self._stop_requested = True
            else:
                self._control_buffer.append(message)

    # -- reporting ---------------------------------------------------------
    def report_progress(self, fraction: float, task: str | None = None) -> None:
        self.publisher.progress(fraction, task)

    def report_status(self, status: AgentStatus, detail: str | None = None) -> None:
        self.publisher.status(status, detail)

    # -- storage -----------------------------------------------------------
    @property
    def connection(self) -> sqlite3.Connection:
        """This process's own writable connection, opened on first use.

        Never inherited. A ``sqlite3.Connection`` is not picklable, and sharing
        one across processes corrupts the database rather than failing cleanly.
        """
        if self.database is None:
            raise AgentError("this agent was given no database", agent=self.name)
        if self._connection is None:
            self._connection = connect_writer(self.database)
        return self._connection

    def append_claims(self, claims: Iterable[Claim]) -> int:
        """Append to the claim log and tell the parent how far the log now runs.

        The claims themselves go to the database, not over the bus: the database
        survives a crash and a queue does not.
        """
        batch = tuple(claims)
        if not batch:
            return 0
        last_seq = append_claims(
            self.connection,
            batch,
            max_pending=self.max_pending_claims,
        )
        self.publisher.claim_batch(len(batch), last_seq)
        return last_seq

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None


class AgentProcess:
    """Base class for anything the pool can spawn.

    Subclasses set :attr:`agent_name` and implement :meth:`execute`. The
    instance is pickled to the child, so whatever a subclass stores on ``self``
    must be picklable — which :meth:`assert_picklable` checks here, in the
    parent, at construction.
    """

    agent_name: str = ""

    def __init__(self, *, name: str | None = None) -> None:
        self.name = name or self.agent_name or type(self).__name__.lower()

    def execute(self, context: AgentContext) -> None:
        """Do the agent's work. Overridden by every real agent."""
        raise NotImplementedError(f"{type(self).__name__} must implement execute()")

    def assert_picklable(self) -> None:
        """Fail now, in the parent, naming the attribute that cannot cross.

        Without this the same mistake fails at spawn time with an opaque pickling
        error that names a type and no attribute, from a stack that is entirely
        inside multiprocessing. Storing a live connection or an open file on an
        agent is an easy and natural thing to do; this makes it a one-line fix
        instead of an afternoon.
        """
        for attribute, value in vars(self).items():
            try:
                pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
            # Pickling failures arrive as several unrelated exception types, so
            # this catches broadly on purpose.
            except Exception as exc:
                raise AgentError(
                    f"attribute {attribute!r} ({type(value).__name__}) cannot be pickled, so "
                    f"this agent cannot be spawned: {exc}",
                    agent=self.name,
                ) from exc
        try:
            pickle.dumps(self, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception as exc:
            raise AgentError(
                f"this agent cannot be pickled as a whole, so it cannot be spawned: {exc}",
                agent=self.name,
            ) from exc


def run_agent(agent: AgentProcess, spec: AgentRuntimeSpec) -> None:
    """The child process entry point.

    Module-level and importable by name, because the ``spawn`` start method
    re-imports this module in the child and looks the target up rather than
    inheriting it.
    """
    raise SystemExit(_run_agent(agent, spec))


def _run_agent(agent: AgentProcess, spec: AgentRuntimeSpec) -> int:
    # Before anything else: this process must not touch the log file directly.
    setup_worker_logging(spec.log_queue, level=spec.config.logging.level)
    logger = logging.getLogger(f"scry.agents.{spec.name}")

    publisher = Publisher(spec.inbox, spec.name)
    heartbeat = Heartbeat(publisher, interval=spec.heartbeat_interval)
    context = AgentContext(
        name=spec.name,
        config=spec.config,
        publisher=publisher,
        logger=logger,
        stop_event=spec.stop_event,
        control=spec.control,
        database=spec.database,
        target_path=spec.target_path,
    )

    heartbeat.start()
    publisher.status(AgentStatus.RUNNING)
    logger.info("agent %s started", spec.name)

    try:
        agent.execute(context)
    except KeyboardInterrupt:
        # Windows delivers CTRL_C_EVENT to the whole process group, so every
        # worker raises this at once. Left unhandled, one Ctrl+C prints eight
        # tracebacks for what was a deliberate user action.
        logger.info("agent %s interrupted", spec.name)
        publisher.status(AgentStatus.IDLE, "interrupted")
        code = EXIT_INTERRUPTED
    # Catching everything is the entire job of this handler: an agent that dies
    # without reporting why leaves the parent an exit code and nothing else.
    except BaseException as exc:
        text = redact("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        logger.error("agent %s crashed: %s", spec.name, exc)
        publisher.error(f"{type(exc).__name__}: {exc}", traceback_text=text)
        publisher.status(AgentStatus.CRASHED, str(exc))
        code = EXIT_CRASH
    else:
        publisher.status(AgentStatus.COMPLETED)
        logger.info("agent %s completed", spec.name)
        code = EXIT_OK
    finally:
        heartbeat.stop()
        context.close()
        # Flush the queue's feeder thread before the process exits. Without this
        # the status or error published moments ago can die with the process,
        # and the parent sees an exit code with no explanation attached.
        _flush(spec.inbox)
        sys.stderr.flush()

    return code


def _flush(q: Any) -> None:
    try:
        q.close()
        q.join_thread()
    except (OSError, ValueError, AttributeError):
        pass
