"""The wire format between the parent process and its workers.

Everything here crosses a process boundary, so everything here is picklable.

**Claims are not on this wire.** Section 1.6's log entry called ``Claim`` the
bus's wire format; section 1.11 narrows that deliberately. A queue is not
durable — a crash loses whatever is in flight, which is precisely what the
append-only claim log exists to prevent. So a worker writes claims to the
database and publishes a :attr:`MessageType.CLAIM_BATCH` notification saying how
many and up to which sequence. The merger drains from the database, the TUI
counts from the notification, and a crash costs nothing that was committed.
``Claim`` remains picklable and may travel here; it simply is not the durable
path.
"""

from __future__ import annotations

import pickle
import uuid
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Any

from scry.util.clock import utc_timestamp
from scry.util.errors import AgentError

# Subscribe to this topic to see every message regardless of its own topic.
TOPIC_ALL = "*"


class MessagePriority(IntEnum):
    """Dispatch order. Lower value sorts first, so no reversing anywhere."""

    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3


class MessageType(StrEnum):
    HEARTBEAT = "heartbeat"
    PROGRESS = "progress"
    STATUS = "status"
    CLAIM_BATCH = "claim_batch"
    ERROR = "error"
    CONTROL = "control"


class AgentStatus(StrEnum):
    """Agent lifecycle states.

    These are exactly the values ``agent_state.status`` accepts in
    ``001_core.sql``. Keeping one vocabulary means the Conductor's rules in
    section 1.12 read the same word from a live message and from the database
    after a restart.
    """

    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    CRASHED = "crashed"
    COMPLETED = "completed"
    ERROR = "error"


class ControlCommand(StrEnum):
    """Parent-to-worker instructions."""

    STOP = "stop"
    PAUSE = "pause"
    RESUME = "resume"
    PRIORITISE = "prioritise"


@dataclass(frozen=True)
class Message:
    """One message on the bus.

    ``payload`` is a plain dict for readability at the call site. It must be
    picklable, which in practice means primitives, tuples and frozen dataclasses
    — the same discipline the config already lives under.
    """

    type: MessageType
    sender: str
    payload: dict[str, Any] = field(default_factory=dict)
    topic: str = ""
    priority: MessagePriority = MessagePriority.NORMAL
    recipient: str | None = None
    correlation_id: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: str = field(default_factory=utc_timestamp)

    def __post_init__(self) -> None:
        # A message with no explicit topic is routed by its type, which is what
        # a subscriber almost always wants. An explicit topic is for the cases
        # that want finer routing than "all errors".
        if not self.topic:
            object.__setattr__(self, "topic", str(self.type))

    # -- factories ---------------------------------------------------------
    # Priorities are set here rather than at every call site so that "errors
    # jump the queue" is a property of the message type instead of a convention
    # each agent has to remember.

    @classmethod
    def heartbeat(cls, sender: str, *, sequence: int) -> Message:
        return cls(
            type=MessageType.HEARTBEAT,
            sender=sender,
            payload={"sequence": sequence},
            priority=MessagePriority.LOW,
        )

    @classmethod
    def progress(cls, sender: str, *, fraction: float, task: str | None = None) -> Message:
        return cls(
            type=MessageType.PROGRESS,
            sender=sender,
            payload={"fraction": max(0.0, min(1.0, float(fraction))), "task": task},
            priority=MessagePriority.LOW,
        )

    @classmethod
    def status(cls, sender: str, *, status: AgentStatus, detail: str | None = None) -> Message:
        return cls(
            type=MessageType.STATUS,
            sender=sender,
            payload={"status": AgentStatus(status), "detail": detail},
            priority=MessagePriority.HIGH,
        )

    @classmethod
    def claim_batch(cls, sender: str, *, count: int, last_seq: int) -> Message:
        return cls(
            type=MessageType.CLAIM_BATCH,
            sender=sender,
            payload={"count": count, "last_seq": last_seq},
            priority=MessagePriority.NORMAL,
        )

    @classmethod
    def error(
        cls,
        sender: str,
        *,
        message: str,
        traceback_text: str | None = None,
        fatal: bool = True,
    ) -> Message:
        return cls(
            type=MessageType.ERROR,
            sender=sender,
            payload={"message": message, "traceback": traceback_text, "fatal": fatal},
            priority=MessagePriority.CRITICAL,
        )

    @classmethod
    def control(
        cls,
        sender: str,
        *,
        command: ControlCommand,
        recipient: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> Message:
        payload: dict[str, Any] = {"command": ControlCommand(command)}
        if detail:
            payload.update(detail)
        return cls(
            type=MessageType.CONTROL,
            sender=sender,
            payload=payload,
            priority=MessagePriority.HIGH,
            recipient=recipient,
        )


def dispatch_key(message: Message) -> int:
    """Sort key for dispatch order.

    Deliberately returns only the priority, and is used with ``list.sort``.

    The obvious implementation — ``heapq`` or ``PriorityQueue`` over
    ``(priority, message)`` tuples — raises ``TypeError: '<' not supported`` the
    moment two messages share a priority, because the tuple comparison falls
    through to the messages themselves. That never happens in a small test and
    always happens under load. Python's sort is stable, so sorting on the
    priority alone keeps arrival order within a priority level and never
    compares a message to a message.
    """
    return int(message.priority)


def encode(message: Message) -> bytes:
    """Serialise a message for the queue.

    Callers put *bytes* on the queue rather than the message itself, even though
    ``multiprocessing.Queue`` would pickle it anyway. The difference is where the
    failure surfaces: the queue pickles on a background feeder thread, so an
    unpicklable payload raises there, prints to the worker's stderr, and the
    message simply never arrives — nothing fails at the call site. Encoding
    eagerly turns that silent disappearance into a traceback pointing at the
    offending ``publish``.
    """
    try:
        return pickle.dumps(message, protocol=pickle.HIGHEST_PROTOCOL)
    except (pickle.PicklingError, TypeError, AttributeError, ValueError) as exc:
        raise AgentError(
            f"message payload is not picklable and could not be sent: {exc}",
            agent=message.sender,
        ) from exc


def decode(data: bytes) -> Message:
    """Deserialise a message from the queue.

    Unpickling arbitrary bytes executes arbitrary code, so it is worth being
    explicit about where these come from: a private pipe between this process and
    a child it spawned itself. Nothing here ever decodes bytes from a file, a
    socket, or a user.
    """
    message = pickle.loads(data)
    if not isinstance(message, Message):
        raise AgentError(f"expected a Message on the bus, got {type(message).__name__}")
    return message
