"""Message routing between the parent process and its workers.

The topology is deliberately asymmetric. Every worker publishes into **one**
shared inbox that the parent drains, while each worker has its **own** control
queue that only the parent writes to. Workers therefore never talk to each other
— all coordination goes through the parent, which is what makes the Conductor in
section 1.12 the single place where scheduling decisions happen, rather than an
emergent property of eight processes negotiating.
"""

from __future__ import annotations

import queue as queue_module
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from scry.runtime.messages import (
    TOPIC_ALL,
    AgentStatus,
    ControlCommand,
    Message,
    decode,
    dispatch_key,
    encode,
)
from scry.util.clock import utc_timestamp

Subscriber = Callable[[Message], None]

# Bounded so a long run cannot accumulate dead letters until it runs out of
# memory. Losing the oldest is the right trade: the first few explain the bug.
DEFAULT_DEAD_LETTER_CAPACITY = 256

# How many messages one drain will take before returning. Without a cap, a
# parent draining a busy queue never gets back to supervising its children.
DEFAULT_DRAIN_LIMIT = 256


@dataclass(frozen=True)
class DeadLetter:
    """A message that reached no one, and why.

    Exists so that "a message went missing" is answerable. Without it the only
    symptom of a routing bug is silence, which is indistinguishable from a worker
    that had nothing to say.
    """

    message: Message
    reason: str
    at: str = field(default_factory=utc_timestamp)


class Publisher:
    """The worker-side half of the bus: a queue and the name to stamp on it.

    Deliberately tiny. It holds no locks, no threads and no state beyond the
    queue, because it is used from the agent's main thread *and* from its
    heartbeat thread at the same time. ``multiprocessing.Queue.put`` is itself
    thread-safe, so there is nothing here that needs protecting.
    """

    def __init__(self, inbox: Any, sender: str) -> None:
        self._inbox = inbox
        self.sender = sender

    def publish(self, message: Message) -> None:
        self._inbox.put(encode(message))

    def heartbeat(self, sequence: int) -> None:
        self.publish(Message.heartbeat(self.sender, sequence=sequence))

    def progress(self, fraction: float, task: str | None = None) -> None:
        self.publish(Message.progress(self.sender, fraction=fraction, task=task))

    def status(self, status: AgentStatus, detail: str | None = None) -> None:
        self.publish(Message.status(self.sender, status=status, detail=detail))

    def claim_batch(self, count: int, last_seq: int) -> None:
        self.publish(Message.claim_batch(self.sender, count=count, last_seq=last_seq))

    def error(self, message: str, traceback_text: str | None = None, fatal: bool = True) -> None:
        self.publish(
            Message.error(self.sender, message=message, traceback_text=traceback_text, fatal=fatal)
        )


class MessageBus:
    """The parent-side inbox, its subscribers, and the dead-letter queue."""

    def __init__(
        self,
        *,
        inbox: Any | None = None,
        context: Any | None = None,
        dead_letter_capacity: int = DEFAULT_DEAD_LETTER_CAPACITY,
    ) -> None:
        import multiprocessing

        self._context = context if context is not None else multiprocessing.get_context("spawn")
        self._inbox = inbox if inbox is not None else self._context.Queue()
        self._subscribers: dict[str, list[Subscriber]] = {}
        self._dead_letters: deque[DeadLetter] = deque(maxlen=dead_letter_capacity)
        self._closed = False

    @property
    def inbox(self) -> Any:
        """The queue handed to workers. Pass this, never the bus itself."""
        return self._inbox

    @property
    def context(self) -> Any:
        return self._context

    def control_queue(self) -> Any:
        """A fresh parent-to-worker queue, one per agent."""
        return self._context.Queue()

    # -- subscription ------------------------------------------------------
    def subscribe(self, topic: str, callback: Subscriber) -> None:
        self._subscribers.setdefault(topic, []).append(callback)

    def subscribers_for(self, topic: str) -> tuple[Subscriber, ...]:
        return tuple(self._subscribers.get(topic, ())) + tuple(self._subscribers.get(TOPIC_ALL, ()))

    # -- receiving ---------------------------------------------------------
    def drain(self, *, timeout: float = 0.0, limit: int = DEFAULT_DRAIN_LIMIT) -> list[Message]:
        """Collect pending messages and return them in dispatch order.

        ``multiprocessing.Queue`` is FIFO and cannot be made priority-ordered
        across a pipe — a pipe delivers bytes in the order they were written and
        no argument changes that. Priority is therefore applied *here*, on a
        drained batch, which is sufficient because the parent is the only thing
        that acts on priority. Claiming cross-process priority ordering would be
        a lie; this is what is actually achievable.
        """
        collected: list[Message] = []
        deadline = time.monotonic() + timeout

        while len(collected) < limit:
            remaining = deadline - time.monotonic()
            try:
                if remaining > 0:
                    raw = self._inbox.get(timeout=remaining)
                else:
                    raw = self._inbox.get_nowait()
            except queue_module.Empty:
                break
            except (OSError, ValueError):
                # The queue was closed underneath us during shutdown.
                break
            collected.append(decode(raw))

        collected.sort(key=dispatch_key)
        return collected

    def dispatch(self, *, timeout: float = 0.0, limit: int = DEFAULT_DRAIN_LIMIT) -> int:
        """Drain and deliver. Returns how many messages were handled."""
        messages = self.drain(timeout=timeout, limit=limit)
        for message in messages:
            self.deliver(message)
        return len(messages)

    def deliver(self, message: Message) -> None:
        """Hand one message to its subscribers, or to the dead-letter queue."""
        callbacks = self.subscribers_for(message.topic)
        if not callbacks:
            self._dead_letters.append(DeadLetter(message, "no subscriber for topic"))
            return

        for callback in callbacks:
            try:
                callback(message)
            except Exception as exc:
                # Supervision is the parent's job; a subscriber that throws must
                # not be able to stop the parent from noticing that a worker
                # died. The failure is recorded rather than raised.
                self._dead_letters.append(
                    DeadLetter(message, f"handler raised {type(exc).__name__}: {exc}")
                )

    # -- sending -----------------------------------------------------------
    def send(self, control: Any, message: Message) -> None:
        """Send a control message down one worker's queue."""
        control.put(encode(message))

    def broadcast(self, controls: Iterable[Any], message: Message) -> None:
        for control in controls:
            self.send(control, message)

    def stop_command(self, recipient: str | None = None) -> Message:
        return Message.control(sender="conductor", command=ControlCommand.STOP, recipient=recipient)

    # -- inspection --------------------------------------------------------
    @property
    def dead_letters(self) -> tuple[DeadLetter, ...]:
        return tuple(self._dead_letters)

    def clear_dead_letters(self) -> None:
        self._dead_letters.clear()

    def close(self) -> None:
        """Close the inbox. Only after every worker has been joined."""
        if self._closed:
            return
        self._closed = True
        try:
            self._inbox.close()
            self._inbox.join_thread()
        except (OSError, ValueError, AttributeError):
            # Already closed, or a queue implementation without a feeder thread.
            pass
