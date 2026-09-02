"""The multiprocessing harness: spawn agents, message them, supervise them.

Section 1.11. Two decisions here shape everything built on top.

**The start method is ``spawn`` on every platform, not just Windows.** Uniform
spawn costs startup time on Linux, where ``fork`` is cheaper, and buys the one
thing that matters more: everything handed to a worker must be picklable
*everywhere*, so a violation fails in CI rather than only on the dev machine
weeks later. It also removes fork's own hazards — an inherited SQLite connection
and an inherited open log file are both silent corruption.

**Mechanism lives here, policy lives in the Conductor.** The pool reports that an
agent crashed; it does not decide whether to restart it. Section 1.12 owns the
rules, which is what lets them be tested without spawning a single process.
"""

from __future__ import annotations

from scry.runtime.agent import (
    EXIT_CRASH,
    EXIT_INTERRUPTED,
    EXIT_OK,
    AgentContext,
    AgentProcess,
    AgentRuntimeSpec,
    run_agent,
)
from scry.runtime.bus import DeadLetter, MessageBus, Publisher
from scry.runtime.heartbeat import AgentLiveness, Heartbeat, HeartbeatMonitor
from scry.runtime.messages import (
    TOPIC_ALL,
    AgentStatus,
    ControlCommand,
    Message,
    MessagePriority,
    MessageType,
    decode,
    encode,
)
from scry.runtime.pool import AgentPool, ManagedAgent

__all__ = [
    "EXIT_CRASH",
    "EXIT_INTERRUPTED",
    "EXIT_OK",
    "TOPIC_ALL",
    "AgentContext",
    "AgentLiveness",
    "AgentPool",
    "AgentProcess",
    "AgentRuntimeSpec",
    "AgentStatus",
    "ControlCommand",
    "DeadLetter",
    "Heartbeat",
    "HeartbeatMonitor",
    "ManagedAgent",
    "Message",
    "MessageBus",
    "MessagePriority",
    "MessageType",
    "Publisher",
    "decode",
    "encode",
    "run_agent",
]
