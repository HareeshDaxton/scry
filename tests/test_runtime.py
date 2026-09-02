"""Section 1.11 — the multiprocessing harness.

Every agent class here is defined at module level. The ``spawn`` start method
re-imports this module in the child and looks the class up by qualified name; a
class defined inside a test function cannot be found that way and fails to
unpickle with an error that points nowhere useful.
"""

from __future__ import annotations

import dataclasses
import logging
import pickle
import time

import pytest

from scry.config import Config
from scry.runtime.agent import (
    EXIT_INTERRUPTED,
    EXIT_OK,
    AgentContext,
    AgentProcess,
)
from scry.runtime.bus import MessageBus, Publisher
from scry.runtime.heartbeat import Heartbeat, HeartbeatMonitor
from scry.runtime.messages import (
    TOPIC_ALL,
    AgentStatus,
    ControlCommand,
    Message,
    MessagePriority,
    MessageType,
    decode,
    dispatch_key,
    encode,
)
from scry.runtime.pool import AgentPool
from scry.storage.claims import Claim
from scry.storage.db import reader
from scry.util.errors import AgentError
from scry.util.logging import setup_logging


def secret_shaped(prefix: str, length: int) -> str:
    """Build a credential-shaped string without one appearing in this source.

    Same rule as ``tests/test_redact.py``: GitHub push protection scans committed
    text and rejects anything credential-shaped, and it sees through same-line
    string concatenation of literals. A repository whose purpose is finding
    secrets cannot contain secret-shaped text.
    """
    return prefix + ("EXAMPLEONLY" + "0" * length)[:length]


# ---------------------------------------------------------------------------
# Test agents
# ---------------------------------------------------------------------------
class CountingAgent(AgentProcess):
    """Publishes a fixed number of progress messages, then finishes."""

    agent_name = "counter"

    def __init__(self, *, name: str | None = None, count: int = 5) -> None:
        super().__init__(name=name)
        self.count = count

    def execute(self, context: AgentContext) -> None:
        for index in range(self.count):
            context.report_progress((index + 1) / self.count, task=f"step-{index}")


class CrashingAgent(AgentProcess):
    agent_name = "crasher"

    def execute(self, context: AgentContext) -> None:
        raise ValueError("deliberate crash, with a distinctive marker: SENTINEL-7Q")


class InterruptedAgent(AgentProcess):
    agent_name = "interrupted"

    def execute(self, context: AgentContext) -> None:
        raise KeyboardInterrupt


class WaitingAgent(AgentProcess):
    """Cooperative: polls should_stop and exits cleanly when asked."""

    agent_name = "waiter"

    def execute(self, context: AgentContext) -> None:
        while not context.should_stop():
            time.sleep(0.02)


class StubbornAgent(AgentProcess):
    """Never checks should_stop, so it has to be terminated."""

    agent_name = "stubborn"

    def execute(self, context: AgentContext) -> None:
        time.sleep(120)


class LoggingAgent(AgentProcess):
    agent_name = "logger"

    def __init__(self, *, name: str | None = None, line: str = "") -> None:
        super().__init__(name=name)
        self.line = line

    def execute(self, context: AgentContext) -> None:
        context.logger.warning("worker says: %s", self.line)


class ClaimingAgent(AgentProcess):
    agent_name = "claimer"

    def __init__(self, *, name: str | None = None, count: int = 3) -> None:
        super().__init__(name=name)
        self.count = count

    def execute(self, context: AgentContext) -> None:
        context.append_claims(
            Claim(
                agent=context.name,
                claim_type="hotspot",
                assertion=f"file {index} churns",
                confidence=0.6,
                target_file=f"src/module_{index}.py",
            )
            for index in range(self.count)
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def config():
    return Config()


@pytest.fixture
def pool(config):
    created = AgentPool(config)
    try:
        yield created
    finally:
        created.shutdown(grace=2.0)


def pump(pool_, predicate, *, timeout=30.0):
    """Poll the pool until ``predicate`` holds. Returns whether it did."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pool_.poll(timeout=0.05)
        if predicate():
            return True
    pool_.poll(timeout=0.0)
    return predicate()


def until_exited(pool_, name, *, timeout=30.0):
    return pump(pool_, lambda: pool_[name].exit_code is not None, timeout=timeout)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------
def test_priority_values_sort_most_urgent_first():
    ordered = sorted(MessagePriority)
    assert ordered[0] is MessagePriority.CRITICAL
    assert ordered[-1] is MessagePriority.LOW


def test_topic_defaults_to_the_message_type():
    assert Message(type=MessageType.STATUS, sender="a").topic == "status"


def test_an_explicit_topic_is_kept():
    message = Message(type=MessageType.STATUS, sender="a", topic="archivist.files")
    assert message.topic == "archivist.files"


def test_errors_outrank_everything_else():
    assert Message.error("a", message="boom").priority is MessagePriority.CRITICAL
    assert Message.status("a", status=AgentStatus.RUNNING).priority is MessagePriority.HIGH
    assert Message.heartbeat("a", sequence=1).priority is MessagePriority.LOW


def test_progress_fraction_is_clamped():
    assert Message.progress("a", fraction=5.0).payload["fraction"] == 1.0
    assert Message.progress("a", fraction=-2.0).payload["fraction"] == 0.0


def test_messages_are_frozen():
    message = Message(type=MessageType.STATUS, sender="a")
    with pytest.raises(dataclasses.FrozenInstanceError):
        message.sender = "b"  # type: ignore[misc]


def test_encode_decode_round_trip():
    original = Message.claim_batch("archivist", count=12, last_seq=99)
    restored = decode(encode(original))
    assert restored == original
    assert restored.payload["count"] == 12


def test_encode_names_the_agent_when_the_payload_cannot_be_pickled():
    message = Message(type=MessageType.STATUS, sender="archivist", payload={"f": lambda: None})
    with pytest.raises(AgentError) as excinfo:
        encode(message)
    assert "archivist" in str(excinfo.value)


def test_decode_rejects_something_that_is_not_a_message():
    with pytest.raises(AgentError):
        decode(pickle.dumps({"not": "a message"}))


def test_agent_status_matches_the_database_vocabulary():
    # These are the values `agent_state.status` accepts in 001_core.sql. One
    # vocabulary, so 1.12 reads the same word from a message and from the row.
    assert {s.value for s in AgentStatus} == {
        "idle",
        "running",
        "paused",
        "crashed",
        "completed",
        "error",
    }


def test_control_carries_its_command_and_recipient():
    message = Message.control("conductor", command=ControlCommand.STOP, recipient="archivist")
    assert message.payload["command"] is ControlCommand.STOP
    assert message.recipient == "archivist"


def test_sorting_tied_priorities_keeps_arrival_order_and_never_compares_messages():
    # The trap this guards: heapq or PriorityQueue over (priority, message)
    # raises TypeError the moment two priorities tie, because the comparison
    # falls through to the messages. Ties never happen in a small test and
    # always happen under load.
    messages = [Message.heartbeat(f"agent-{i}", sequence=i) for i in range(5)]
    messages.insert(3, Message.error("late", message="boom"))
    messages.sort(key=dispatch_key)
    assert messages[0].sender == "late"
    assert [m.sender for m in messages[1:]] == [f"agent-{i}" for i in range(5)]


# ---------------------------------------------------------------------------
# Bus
# ---------------------------------------------------------------------------
def test_subscriber_receives_its_topic():
    bus = MessageBus()
    seen = []
    bus.subscribe("status", seen.append)
    bus.deliver(Message.status("a", status=AgentStatus.RUNNING))
    assert len(seen) == 1
    bus.close()


def test_wildcard_subscriber_sees_every_topic():
    bus = MessageBus()
    seen = []
    bus.subscribe(TOPIC_ALL, seen.append)
    bus.deliver(Message.status("a", status=AgentStatus.RUNNING))
    bus.deliver(Message.heartbeat("a", sequence=1))
    assert len(seen) == 2
    bus.close()


def test_a_message_with_no_subscriber_becomes_a_dead_letter():
    bus = MessageBus()
    bus.deliver(Message.status("a", status=AgentStatus.RUNNING))
    assert len(bus.dead_letters) == 1
    assert "no subscriber" in bus.dead_letters[0].reason
    bus.close()


def test_a_handler_that_raises_is_recorded_and_the_others_still_run():
    bus = MessageBus()
    seen = []

    def explode(_message):
        raise RuntimeError("handler is broken")

    bus.subscribe("status", explode)
    bus.subscribe("status", seen.append)
    bus.deliver(Message.status("a", status=AgentStatus.RUNNING))

    assert seen, "a broken handler must not stop the rest"
    assert "RuntimeError" in bus.dead_letters[0].reason
    bus.close()


def test_dead_letters_are_bounded():
    bus = MessageBus(dead_letter_capacity=3)
    for index in range(10):
        bus.deliver(Message.heartbeat(f"a{index}", sequence=index))
    assert len(bus.dead_letters) == 3
    bus.close()


def test_clearing_dead_letters():
    bus = MessageBus()
    bus.deliver(Message.heartbeat("a", sequence=1))
    bus.clear_dead_letters()
    assert bus.dead_letters == ()
    bus.close()


def pump_queue(bus, *, expected, timeout=10.0):
    """Drain until ``expected`` messages have arrived, or time runs out."""
    collected = []
    deadline = time.monotonic() + timeout
    while len(collected) < expected and time.monotonic() < deadline:
        collected.extend(bus.drain(timeout=0.05))
    collected.sort(key=dispatch_key)
    return collected


def test_drain_returns_priority_order_not_arrival_order():
    bus = MessageBus()
    publisher = Publisher(bus.inbox, "a")
    publisher.heartbeat(1)
    publisher.progress(0.5)
    publisher.error("boom")
    publisher.status(AgentStatus.RUNNING)

    drained = pump_queue(bus, expected=4)
    assert [m.type for m in drained][:2] == [MessageType.ERROR, MessageType.STATUS]
    bus.close()


def test_drain_honours_its_limit():
    bus = MessageBus()
    publisher = Publisher(bus.inbox, "a")
    for index in range(10):
        publisher.heartbeat(index)

    # One drain must not consume more than its limit, however much is waiting.
    collected = []
    deadline = time.monotonic() + 10.0
    while len(collected) < 10 and time.monotonic() < deadline:
        batch = bus.drain(timeout=0.05, limit=4)
        assert len(batch) <= 4
        collected.extend(batch)
    assert len(collected) == 10
    bus.close()


def test_drain_returns_nothing_when_the_queue_is_empty():
    bus = MessageBus()
    assert bus.drain(timeout=0.05) == []
    bus.close()


def test_publisher_error_carries_its_traceback():
    bus = MessageBus()
    Publisher(bus.inbox, "a").error("boom", traceback_text="Traceback...")
    drained = pump_queue(bus, expected=1)
    assert drained[0].payload["traceback"] == "Traceback..."
    bus.close()


def test_control_messages_travel_down_a_worker_queue():
    bus = MessageBus()
    control = bus.control_queue()
    bus.send(control, bus.stop_command(recipient="archivist"))
    raw = control.get(timeout=5)
    assert decode(raw).payload["command"] is ControlCommand.STOP
    bus.close()


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------
class RecordingPublisher(Publisher):
    def __init__(self):
        self.beats = []

    def heartbeat(self, sequence):
        self.beats.append(sequence)


def test_heartbeat_beats_immediately_on_start():
    publisher = RecordingPublisher()
    beat = Heartbeat(publisher, interval=60)
    beat.start()
    try:
        # Without an immediate beat the monitor has no observation for a whole
        # interval after spawn - exactly when an agent is most likely to fail.
        assert publisher.beats == [1]
    finally:
        beat.stop()


def test_heartbeat_stops_without_waiting_out_its_interval():
    publisher = RecordingPublisher()
    beat = Heartbeat(publisher, interval=30)
    beat.start()
    started = time.monotonic()
    beat.stop()
    # Event.wait rather than time.sleep is what makes this instant.
    assert time.monotonic() - started < 2.0


def test_heartbeat_survives_a_publisher_that_fails():
    class BrokenPublisher(RecordingPublisher):
        def heartbeat(self, sequence):
            raise OSError("queue is closed")

    beat = Heartbeat(BrokenPublisher(), interval=0.01)
    beat.start()  # must not raise: the parent going away is normal at shutdown
    beat.stop()


def test_monitor_reports_an_agent_whose_heartbeat_stopped():
    now = [1000.0]
    monitor = HeartbeatMonitor(heartbeat_timeout=30, clock=lambda: now[0])
    monitor.register("archivist")
    now[0] += 10
    assert monitor.silent() == ()
    now[0] += 25
    assert monitor.silent() == ("archivist",)


def test_a_heartbeat_clears_the_silence():
    now = [1000.0]
    monitor = HeartbeatMonitor(heartbeat_timeout=30, clock=lambda: now[0])
    monitor.register("archivist")
    now[0] += 40
    monitor.record_heartbeat("archivist")
    assert monitor.silent() == ()
    assert monitor.liveness("archivist").heartbeats == 1


def test_a_beating_agent_that_makes_no_progress_is_stalled_not_silent():
    # The distinction 1.12's agent_stalled rule depends on. The heartbeat runs on
    # its own thread, so a wedged agent keeps beating perfectly.
    now = [1000.0]
    monitor = HeartbeatMonitor(heartbeat_timeout=30, stall_timeout=100, clock=lambda: now[0])
    monitor.register("archivist")
    for _ in range(20):
        now[0] += 10
        monitor.record_heartbeat("archivist")

    assert monitor.silent() == ()
    assert monitor.stalled() == ("archivist",)


def test_progress_refreshes_both_clocks():
    now = [1000.0]
    monitor = HeartbeatMonitor(heartbeat_timeout=30, stall_timeout=100, clock=lambda: now[0])
    monitor.register("archivist")
    now[0] += 200
    monitor.record_progress("archivist", 0.5, "blaming")
    assert monitor.silent() == ()
    assert monitor.stalled() == ()
    assert monitor.liveness("archivist").progress_fraction == 0.5


def test_a_silent_agent_is_not_also_reported_as_stalled():
    now = [1000.0]
    monitor = HeartbeatMonitor(heartbeat_timeout=30, stall_timeout=100, clock=lambda: now[0])
    monitor.register("archivist")
    now[0] += 500
    assert monitor.silent() == ("archivist",)
    assert monitor.stalled() == ()


def test_forgetting_an_agent_removes_it():
    monitor = HeartbeatMonitor()
    monitor.register("archivist")
    monitor.forget("archivist")
    assert monitor.snapshot() == {}


# ---------------------------------------------------------------------------
# Agent construction
# ---------------------------------------------------------------------------
class LeakyAgent(AgentProcess):
    agent_name = "leaky"

    def __init__(self, handle):
        super().__init__()
        self.handle = handle

    def execute(self, context: AgentContext) -> None:  # pragma: no cover - never spawned
        pass


def test_an_unpicklable_attribute_is_caught_in_the_parent(tmp_path):
    # Without this check the same mistake fails at spawn time with a pickling
    # error that names a type, no attribute, and a stack entirely inside
    # multiprocessing.
    path = tmp_path / "open.txt"
    path.write_text("x", encoding="utf-8")
    with path.open() as handle:
        agent = LeakyAgent(handle)
        with pytest.raises(AgentError) as excinfo:
            agent.assert_picklable()

    message = str(excinfo.value)
    assert "handle" in message
    assert "leaky" in message


def test_a_well_behaved_agent_passes_the_pickle_check():
    CountingAgent(count=3).assert_picklable()


def test_execute_must_be_implemented():
    with pytest.raises(NotImplementedError):
        AgentProcess(name="bare").execute(None)  # type: ignore[arg-type]


def test_an_agent_takes_its_class_name_by_default():
    assert CountingAgent().name == "counter"
    assert CountingAgent(name="counter-2").name == "counter-2"


# ---------------------------------------------------------------------------
# AgentContext
# ---------------------------------------------------------------------------
@pytest.fixture
def bus():
    created = MessageBus()
    try:
        yield created
    finally:
        created.close()


def make_context(config, bus_, *, control=None, stop_event=None):
    """An AgentContext as a child would build it, but in this process."""
    return AgentContext(
        name="test",
        config=config,
        publisher=Publisher(bus_.inbox, "test"),
        logger=logging.getLogger("scry.agents.test"),
        stop_event=stop_event if stop_event is not None else bus_.context.Event(),
        control=control if control is not None else bus_.control_queue(),
    )


def wait_for(predicate, *, timeout=10.0):
    """Queues are asynchronous; a put is not visible to a get straight away."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_should_stop_follows_the_stop_event(config, bus):
    event = bus.context.Event()
    context = make_context(config, bus, stop_event=event)
    assert not context.should_stop()
    event.set()
    assert context.should_stop()


def test_should_stop_follows_a_stop_control_message(config, bus):
    control = bus.control_queue()
    context = make_context(config, bus, control=control)
    bus.send(control, bus.stop_command())
    assert wait_for(context.should_stop)


def test_poll_control_returns_other_commands_and_then_clears(config, bus):
    control = bus.control_queue()
    context = make_context(config, bus, control=control)
    bus.send(control, Message.control("conductor", command=ControlCommand.PRIORITISE))

    assert wait_for(lambda: bool(context.poll_control() or context._control_buffer))
    # The first successful poll consumed it, so the buffer is now empty.
    assert context.poll_control() == ()


def test_a_stop_message_is_not_returned_as_an_ordinary_command(config, bus):
    control = bus.control_queue()
    context = make_context(config, bus, control=control)
    bus.send(control, bus.stop_command())
    assert wait_for(context.should_stop)
    assert context.poll_control() == ()


def test_an_agent_with_no_database_says_so(config, bus):
    context = make_context(config, bus)
    with pytest.raises(AgentError):
        _ = context.connection


def test_appending_no_claims_does_nothing(config, bus):
    context = make_context(config, bus)
    assert context.append_claims([]) == 0


# ---------------------------------------------------------------------------
# Pool — spawning and supervision
# ---------------------------------------------------------------------------
@pytest.mark.slow
def test_an_agent_runs_to_completion(pool):
    pool.spawn(CountingAgent(count=3))
    assert until_exited(pool, "counter")
    assert pool["counter"].exit_code == EXIT_OK
    assert pool["counter"].status is AgentStatus.COMPLETED
    assert pool.crashed() == ()


@pytest.mark.slow
def test_progress_from_a_worker_reaches_the_monitor(pool):
    pool.spawn(CountingAgent(count=4))
    assert until_exited(pool, "counter")
    liveness = pool.monitor.liveness("counter")
    assert liveness.progress_fraction == pytest.approx(1.0)
    assert liveness.heartbeats >= 1


@pytest.mark.slow
def test_a_crash_is_detected_with_its_traceback(pool):
    pool.spawn(CrashingAgent())
    assert until_exited(pool, "crasher")

    managed = pool["crasher"]
    assert managed.status is AgentStatus.CRASHED
    assert managed.crash_count == 1
    assert managed.exit_code != EXIT_OK
    assert "ValueError" in (managed.last_error or "")
    # An exit code alone is not diagnosable; the traceback has to survive the
    # process that produced it.
    assert "SENTINEL-7Q" in (managed.last_traceback or "")
    assert pool.crashed() == ("crasher",)


@pytest.mark.slow
def test_an_interrupt_is_not_counted_as_a_crash(pool):
    # Three Ctrl+Cs must not trip 1.12's three-strikes rule and permanently
    # disable an agent that never actually failed.
    pool.spawn(InterruptedAgent())
    assert until_exited(pool, "interrupted")

    managed = pool["interrupted"]
    assert managed.exit_code == EXIT_INTERRUPTED
    assert managed.interrupted is True
    assert managed.crash_count == 0
    assert managed.status is AgentStatus.COMPLETED


@pytest.mark.slow
def test_a_cooperative_agent_exits_cleanly_on_shutdown(config):
    created = AgentPool(config)
    try:
        created.spawn(WaitingAgent())
        pump(created, lambda: created.monitor.liveness("waiter").heartbeats >= 1, timeout=20)
        created.shutdown(grace=10.0)

        # Exit code zero means it left through the top of execute(), not through
        # terminate() with a transaction half written.
        assert created["waiter"].exit_code == EXIT_OK
        assert created["waiter"].crash_count == 0
    finally:
        created.shutdown(grace=1.0)


@pytest.mark.slow
def test_an_agent_that_ignores_the_stop_request_is_terminated_but_not_blamed(config):
    created = AgentPool(config)
    try:
        created.spawn(StubbornAgent())
        pump(created, lambda: created.monitor.liveness("stubborn").heartbeats >= 1, timeout=20)
        created.shutdown(grace=0.5)

        managed = created["stubborn"]
        assert not managed.alive
        # terminate() gives -SIGTERM on POSIX and 1 on Windows. Neither is a
        # crash: we asked for it.
        assert managed.crash_count == 0
    finally:
        created.shutdown(grace=1.0)


@pytest.mark.slow
def test_the_pool_is_a_context_manager(config):
    with AgentPool(config) as created:
        created.spawn(CountingAgent(count=2))
        until_exited(created, "counter")
    assert created["counter"].exit_code == EXIT_OK


@pytest.mark.slow
def test_killing_one_agent_leaves_the_others_running(config):
    created = AgentPool(config)
    try:
        created.spawn(StubbornAgent(name="stubborn-a"))
        created.spawn(StubbornAgent(name="stubborn-b"))
        pump(created, lambda: len(created.running()) == 2, timeout=20)

        created.kill("stubborn-a")
        assert not created["stubborn-a"].alive
        assert created["stubborn-b"].alive
    finally:
        created.shutdown(grace=0.5)


def test_the_concurrency_cap_refuses_rather_than_queueing(config):
    # Deciding which agent runs when a slot frees is scheduling, and scheduling
    # is the Conductor's job in 1.12. A queue here would be a second scheduler
    # with no rules.
    created = AgentPool(config, max_concurrent=1)
    try:
        created.spawn(WaitingAgent())
        with pytest.raises(AgentError) as excinfo:
            created.spawn(CountingAgent())
        assert "concurrency cap" in str(excinfo.value)
    finally:
        created.shutdown(grace=2.0)


def test_two_agents_cannot_share_a_name(config):
    created = AgentPool(config)
    try:
        created.spawn(WaitingAgent())
        with pytest.raises(AgentError):
            created.spawn(WaitingAgent())
    finally:
        created.shutdown(grace=2.0)


def test_addressing_an_unknown_agent_is_an_error(config):
    created = AgentPool(config)
    try:
        with pytest.raises(AgentError):
            created.send("nobody", Message.status("x", status=AgentStatus.IDLE))
    finally:
        created.shutdown()


def test_an_unpicklable_agent_never_reaches_spawn(config, tmp_path):
    path = tmp_path / "open.txt"
    path.write_text("x", encoding="utf-8")
    created = AgentPool(config)
    try:
        with path.open() as handle, pytest.raises(AgentError):
            created.spawn(LeakyAgent(handle))
        assert len(created) == 0
    finally:
        created.shutdown()


@pytest.mark.slow
def test_eight_agents_exchange_a_thousand_messages(config):
    # The section's headline acceptance check.
    created = AgentPool(config, max_concurrent=8)
    progress: list[Message] = []
    created.bus.subscribe(str(MessageType.PROGRESS), progress.append)

    try:
        for index in range(8):
            created.spawn(CountingAgent(name=f"counter-{index}", count=125))

        pump(created, lambda: len(progress) >= 1000, timeout=90)
        created.shutdown(grace=10.0)

        assert len(progress) == 1000
        assert all(m.exit_code == EXIT_OK for m in created.agents)
        assert created.crashed() == ()
    finally:
        created.shutdown(grace=1.0)


@pytest.mark.slow
def test_one_crash_among_many_is_isolated(config):
    created = AgentPool(config, max_concurrent=8)
    try:
        for index in range(3):
            created.spawn(CountingAgent(name=f"counter-{index}", count=5))
        created.spawn(CrashingAgent())

        pump(created, lambda: all(m.exit_code is not None for m in created.agents), timeout=45)

        assert created.crashed() == ("crasher",)
        assert created.statuses()["counter-0"] is AgentStatus.COMPLETED
    finally:
        created.shutdown(grace=2.0)


# ---------------------------------------------------------------------------
# Worker logging
# ---------------------------------------------------------------------------
@pytest.mark.slow
def test_a_worker_log_line_reaches_the_parents_file(config, tmp_path):
    log_dir = tmp_path / "logs"
    setup_logging(config, log_dir=log_dir, console=False)

    created = AgentPool(config)
    try:
        created.spawn(LoggingAgent(line="hello from the worker"))
        until_exited(created, "logger")
    finally:
        # Workers first, relay second. Stopping the relay first discards
        # whatever the last worker logged on its way out.
        created.shutdown(grace=5.0)

    text = (log_dir / "scry.log").read_text(encoding="utf-8")
    assert text.count("hello from the worker") == 1
    # The parent's formatter supplied the prefix, so the line is not doubled up.
    assert "scry.agents.logger" in text


@pytest.mark.slow
def test_a_secret_logged_in_a_worker_arrives_redacted(config, tmp_path):
    log_dir = tmp_path / "logs"
    setup_logging(config, log_dir=log_dir, console=False)
    credential = secret_shaped("AKIA", 16)

    created = AgentPool(config)
    try:
        created.spawn(LoggingAgent(line=credential))
        until_exited(created, "logger")
    finally:
        created.shutdown(grace=5.0)

    text = (log_dir / "scry.log").read_text(encoding="utf-8")
    # Redaction happens on the worker's QueueHandler, so the value never sat in
    # a pipe buffer unredacted in the first place.
    assert credential not in text
    assert "<REDACTED_aws_key>" in text


# ---------------------------------------------------------------------------
# Claims from a worker
# ---------------------------------------------------------------------------
@pytest.mark.slow
def test_a_worker_writes_claims_to_the_database_not_the_bus(config, initialised_workspace):
    database = initialised_workspace.paths.database
    created = AgentPool(config, database=database)
    try:
        created.spawn(ClaimingAgent(count=4))
        assert until_exited(created, "claimer")
        created.poll()

        # The bus carried only the notification.
        managed = created["claimer"]
        assert managed.claims_appended == 4
        assert managed.last_claim_seq == 4
    finally:
        created.shutdown(grace=5.0)

    # The claims themselves are durable, which a queue would not have been.
    with reader(database) as connection:
        rows = connection.execute("SELECT payload FROM claim_log ORDER BY seq").fetchall()
    assert len(rows) == 4
    assert "src/module_0.py" in rows[0]["payload"]
