"""Tests for ``localpost.threadtools.ThreadTaskGroup``."""

import threading
import time
from concurrent.futures import Future

import pytest

from localpost import threadtools
from localpost.threadtools import ThreadTaskGroup


@pytest.fixture
def fast_idle_timeout(monkeypatch: pytest.MonkeyPatch):
    """Make idle-timeout tests fast: 100 ms instead of 60 s."""
    monkeypatch.setattr(threadtools, "_IDLE_TIMEOUT", 0.1)
    yield 0.1


def _drain_idle_workers() -> None:
    """Wait for the global idle deque to empty.

    Tests that monkeypatch ``_IDLE_TIMEOUT`` to a small value rely on this
    so they don't leak workers (or interact with each other through the
    shared ``_idle`` deque).
    """
    deadline = time.monotonic() + 2.0
    while threadtools._idle and time.monotonic() < deadline:
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# Basic submit / result
# ---------------------------------------------------------------------------


def test_start_soon_returns_future_with_result():
    with ThreadTaskGroup() as tg:
        fut = tg.start_soon(lambda x: x * 2, 21)
        assert isinstance(fut, Future)
        assert fut.result(timeout=5) == 42


def test_start_soon_with_kwargs():
    with ThreadTaskGroup() as tg:
        fut = tg.start_soon(lambda *, a, b: a + b, a=1, b=2)
        assert fut.result(timeout=5) == 3


def test_many_concurrent_tasks_all_complete():
    n = 50

    def work(i: int) -> int:
        time.sleep(0.01)
        return i * i

    with ThreadTaskGroup() as tg:
        futs = [tg.start_soon(work, i) for i in range(n)]

    assert sorted(f.result() for f in futs) == [i * i for i in range(n)]


# ---------------------------------------------------------------------------
# Exception capture
# ---------------------------------------------------------------------------


def test_task_exception_captured_in_future_and_raised_at_exit():
    class Boom(Exception):
        pass

    def bad():
        raise Boom("nope")

    with pytest.raises(ExceptionGroup) as ei:
        with ThreadTaskGroup() as tg:
            fut = tg.start_soon(bad)
            # Future records the exception too
            with pytest.raises(Boom):
                fut.result(timeout=5)
    assert len(ei.value.exceptions) == 1
    assert isinstance(ei.value.exceptions[0], Boom)


def test_body_exception_alone_is_wrapped():
    class BodyBoom(Exception):
        pass

    with pytest.raises(ExceptionGroup) as ei:
        with ThreadTaskGroup():
            raise BodyBoom("body")
    assert len(ei.value.exceptions) == 1
    assert isinstance(ei.value.exceptions[0], BodyBoom)


def test_body_and_task_exceptions_are_merged():
    class Body(Exception):
        pass

    class Task(Exception):
        pass

    def bad():
        raise Task("task")

    with pytest.raises(ExceptionGroup) as ei:
        with ThreadTaskGroup() as tg:
            tg.start_soon(bad)
            time.sleep(0.05)  # let the task land
            raise Body("body")
    types = {type(e) for e in ei.value.exceptions}
    assert types == {Body, Task}


def test_named_group_uses_name_in_exception_message():
    def bad():
        raise RuntimeError("x")

    with pytest.raises(ExceptionGroup, match="ThreadTaskGroup 'pool-x' failed"):
        with ThreadTaskGroup(name="pool-x") as tg:
            tg.start_soon(bad)


# ---------------------------------------------------------------------------
# Drain semantics
# ---------------------------------------------------------------------------


def test_exit_blocks_until_all_tasks_complete():
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def slow():
        started.set()
        release.wait(timeout=5)
        finished.set()

    with ThreadTaskGroup() as tg:
        tg.start_soon(slow)
        assert started.wait(timeout=2)
        assert not finished.is_set()
        release.set()
    # Past the with block: drain has completed.
    assert finished.is_set()


def test_nested_start_soon_during_drain():
    """A task can spawn more tasks; drain waits for the whole transitive set."""
    counter = 0
    counter_lock = threading.Lock()

    def leaf():
        nonlocal counter
        with counter_lock:
            counter += 1

    def branch(tg: ThreadTaskGroup, depth: int):
        if depth == 0:
            leaf()
            return
        # Each branch spawns 2 children of depth-1, waits for them.
        f1 = tg.start_soon(branch, tg, depth - 1)
        f2 = tg.start_soon(branch, tg, depth - 1)
        f1.result(timeout=5)
        f2.result(timeout=5)
        leaf()

    with ThreadTaskGroup() as tg:
        tg.start_soon(branch, tg, 3)

    # depth=3 → 1 + 2 + 4 + 8 = 15 leaves
    assert counter == 15


def test_start_soon_after_close_raises():
    tg = ThreadTaskGroup()
    with tg:
        tg.start_soon(lambda: None).result(timeout=5)
    with pytest.raises(RuntimeError, match="closed"):
        tg.start_soon(lambda: None)


def test_group_cannot_be_reused():
    tg = ThreadTaskGroup()
    with tg:
        pass
    with pytest.raises(RuntimeError, match="cannot be reused"):
        with tg:
            pass


# ---------------------------------------------------------------------------
# Worker reuse / shared pool
# ---------------------------------------------------------------------------


def test_workers_are_shared_across_groups(fast_idle_timeout):
    """A worker idle from one group should be reused by another."""
    seen: set[int] = set()

    def record():
        seen.add(threading.get_ident())

    with ThreadTaskGroup() as tg:
        for _ in range(4):
            tg.start_soon(record).result(timeout=5)
    first_run = set(seen)

    # Second group sees at least some of the same threads (workers parked
    # in the global _idle deque).
    seen.clear()
    with ThreadTaskGroup() as tg:
        for _ in range(4):
            tg.start_soon(record).result(timeout=5)
    second_run = set(seen)

    assert first_run & second_run, "no worker reuse across groups"
    _drain_idle_workers()


def test_idle_workers_self_exit_on_timeout(fast_idle_timeout):
    # Drop any leftover workers from earlier tests — they were spawned before
    # the monkeypatch and are stuck on their original 60 s ``inbox.get``.
    # Orphaning them is safe (daemon threads, won't be reused).
    threadtools._idle.clear()

    # Spawn a fresh worker and grab a reference to it.
    with ThreadTaskGroup() as tg:
        tg.start_soon(lambda: None).result(timeout=5)
    assert len(threadtools._idle) >= 1
    fresh = threadtools._idle[-1]  # LIFO: most recently parked

    # Wait past the idle timeout.
    time.sleep(fast_idle_timeout * 10)
    assert fresh._alive is False

    # Submitting again skips the dead tombstone and spawns fresh.
    with ThreadTaskGroup() as tg:
        tg.start_soon(lambda: None).result(timeout=5)
    _drain_idle_workers()


# ---------------------------------------------------------------------------
# Race / stress
# ---------------------------------------------------------------------------


def test_claim_vs_idle_timeout_race(fast_idle_timeout):
    """Stress the pop+claim vs self-die race.

    Idle workers may time out *exactly* as a dispatcher pops them. The
    dispatcher must either (a) successfully claim a still-alive worker,
    or (b) see a tombstone and try the next one / spawn fresh. No task
    should be lost.
    """
    n = 200
    counter = 0
    lock = threading.Lock()

    def tick():
        nonlocal counter
        with lock:
            counter += 1

    for _ in range(5):
        with ThreadTaskGroup() as tg:
            futs = [tg.start_soon(tick) for _ in range(n)]
        for f in futs:
            f.result(timeout=5)
        # Sleep to let some workers idle-time-out, leaving tombstones.
        time.sleep(fast_idle_timeout * 1.5)

    assert counter == n * 5
    _drain_idle_workers()


def test_start_soon_from_arbitrary_thread():
    """``start_soon`` must be safe to call from any thread."""
    results: list[int] = []
    results_lock = threading.Lock()

    def producer(tg: ThreadTaskGroup):
        for i in range(10):
            tg.start_soon(_record, i, results, results_lock)

    with ThreadTaskGroup() as tg:
        threads = [threading.Thread(target=producer, args=(tg,)) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert sorted(results) == sorted(list(range(10)) * 5)


def _record(i: int, sink: list[int], lock: threading.Lock) -> None:
    with lock:
        sink.append(i)
