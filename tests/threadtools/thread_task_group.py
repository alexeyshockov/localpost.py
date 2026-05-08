"""Tests for ``localpost.threadtools.TaskGroup``.

These tests rely on an ambient ``thread_pool()``, supplied autouse by
``conftest.py``. The pool itself runs on a blocking-portal-backed event
loop so the sync test bodies remain sync.
"""

import contextvars
import threading
import time
from concurrent.futures import Future

import pytest
from anyio.from_thread import BlockingPortal

from localpost.threadtools import TaskGroup, ThreadPool
from localpost.threadtools import _task_group as _tg


@pytest.fixture
def fast_idle_timeout(monkeypatch: pytest.MonkeyPatch):
    """Make idle-timeout tests fast: 100 ms instead of 60 s."""
    monkeypatch.setattr(_tg, "IDLE_TIMEOUT", 0.1)
    return 0.1


def _drain_idle(pool: ThreadPool) -> None:
    """Wait for the pool's idle deque to empty.

    Tests that monkeypatch ``IDLE_TIMEOUT`` to a small value rely on this
    so they don't leak workers (or interact with each other through the
    shared ``idle`` deque).
    """
    deadline = time.monotonic() + 2.0
    while pool.idle and time.monotonic() < deadline:
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# Basic submit / result
# ---------------------------------------------------------------------------


def test_start_soon_returns_none():
    """``start_soon`` is fire-and-forget — explicit ``None`` return."""
    with TaskGroup() as tg:
        result = tg.start_soon(lambda: 42)
    assert result is None


def test_create_task_returns_future_with_result():
    with TaskGroup() as tg:
        fut = tg.create_task(lambda x: x * 2, 21)
        assert isinstance(fut, Future)
        assert fut.result(timeout=5) == 42


def test_create_task_with_kwargs():
    with TaskGroup() as tg:
        fut = tg.create_task(lambda *, a, b: a + b, a=1, b=2)
        assert fut.result(timeout=5) == 3


def test_many_concurrent_tasks_all_complete():
    n = 50

    def work(i: int) -> int:
        time.sleep(0.01)
        return i * i

    with TaskGroup() as tg:
        futs = [tg.create_task(work, i) for i in range(n)]

    assert sorted(f.result() for f in futs) == [i * i for i in range(n)]


# ---------------------------------------------------------------------------
# Construction outside thread_pool() raises
# ---------------------------------------------------------------------------


def test_taskgroup_without_pool_raises():
    """Without an active ``thread_pool()`` context, ``TaskGroup()`` must
    raise rather than silently spawning detached workers.

    The autouse ``pool`` fixture sets the ambient pool for this module;
    we briefly pop it via a fresh contextvars context to simulate the
    no-pool case."""
    ctx = contextvars.Context()

    def attempt():
        with pytest.raises(RuntimeError, match="thread_pool"):
            TaskGroup()

    ctx.run(attempt)


# ---------------------------------------------------------------------------
# Exception capture
# ---------------------------------------------------------------------------


def test_create_task_exception_captured_in_future_and_raised_at_exit():
    class Boom(Exception):
        pass

    def bad():
        raise Boom("nope")

    with pytest.raises(ExceptionGroup) as ei:  # noqa: PT012
        with TaskGroup() as tg:
            fut = tg.create_task(bad)
            # Future records the exception too — and consuming it does
            # *not* suppress the group's ExceptionGroup at __exit__.
            with pytest.raises(Boom):
                fut.result(timeout=5)
    assert len(ei.value.exceptions) == 1
    assert isinstance(ei.value.exceptions[0], Boom)


def test_start_soon_exception_surfaces_in_group():
    """Even fire-and-forget tasks have their errors escalate to the group."""

    class Boom(Exception):
        pass

    def bad():
        raise Boom("nope")

    with pytest.raises(ExceptionGroup) as ei:
        with TaskGroup() as tg:
            tg.start_soon(bad)

    assert len(ei.value.exceptions) == 1
    assert isinstance(ei.value.exceptions[0], Boom)


def test_create_task_result_reraise_is_deduped_in_group():
    """When ``Future.result()`` re-raises a task exception into the body,
    ``__exit__`` collapses the body copy with the group-recorded copy
    (same instance) — surfacing the failure exactly once."""

    class Boom(Exception):
        pass

    def bad():
        raise Boom("nope")

    with pytest.raises(ExceptionGroup) as ei:
        with TaskGroup() as tg:
            tg.create_task(bad).result(timeout=5)

    assert len(ei.value.exceptions) == 1
    assert isinstance(ei.value.exceptions[0], Boom)


def test_distinct_body_and_task_exceptions_are_both_surfaced():
    """Dedup is by identity — two different exception instances must both
    appear in the ExceptionGroup, even if they're the same type."""

    class Boom(Exception):
        pass

    def bad():
        raise Boom("from-task")

    with pytest.raises(ExceptionGroup) as ei:  # noqa: PT012
        with TaskGroup() as tg:
            tg.start_soon(bad)
            time.sleep(0.05)  # let the task land in _errors
            raise Boom("from-body")

    assert len(ei.value.exceptions) == 2
    messages = sorted(str(e) for e in ei.value.exceptions)
    assert messages == ["from-body", "from-task"]


def test_dedup_preserves_recorded_order_when_body_reraises_task_error():
    """If the body re-raises a task exception that wasn't first in
    ``_errors``, dedup must keep it at its original recorded position
    rather than promoting it to the front."""

    class A(Exception):
        pass

    class B(Exception):
        pass

    def task_a():
        raise A("a")

    def task_b():
        raise B("b")

    with pytest.raises(ExceptionGroup) as ei:  # noqa: PT012
        with TaskGroup() as tg:
            tg.start_soon(task_a)
            time.sleep(0.05)  # ensure A lands in _errors first
            fut_b = tg.create_task(task_b)
            # Pull B's exception (same instance now sitting in _errors)
            # and re-raise it into the body. Without dedup-with-order,
            # B would be moved to position 0.
            b_exc = fut_b.exception(timeout=5)
            assert b_exc is not None
            raise b_exc

    types = [type(e) for e in ei.value.exceptions]
    assert types == [A, B]  # record order, not body-first


def test_body_exception_alone_is_wrapped():
    class BodyBoom(Exception):
        pass

    with pytest.raises(ExceptionGroup) as ei:
        with TaskGroup():
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

    with pytest.raises(ExceptionGroup) as ei:  # noqa: PT012
        with TaskGroup() as tg:
            tg.start_soon(bad)
            time.sleep(0.05)  # let the task land
            raise Body("body")
    types = {type(e) for e in ei.value.exceptions}
    assert types == {Body, Task}


def test_named_group_uses_name_in_exception_message():
    def bad():
        raise RuntimeError("x")

    with pytest.raises(ExceptionGroup, match="TaskGroup 'pool-x' failed"):
        with TaskGroup(name="pool-x") as tg:
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

    with TaskGroup() as tg:
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

    def branch(tg: TaskGroup, depth: int):
        if depth == 0:
            leaf()
            return
        # Each branch spawns 2 children of depth-1, waits for them.
        f1 = tg.create_task(branch, tg, depth - 1)
        f2 = tg.create_task(branch, tg, depth - 1)
        f1.result(timeout=5)
        f2.result(timeout=5)
        leaf()

    with TaskGroup() as tg:
        tg.start_soon(branch, tg, 3)

    # depth=3 → 1 + 2 + 4 + 8 = 15 leaves
    assert counter == 15


def test_start_soon_after_close_raises():
    tg = TaskGroup()
    with tg:
        tg.create_task(lambda: None).result(timeout=5)
    with pytest.raises(RuntimeError, match="closed"):
        tg.start_soon(lambda: None)


def test_group_cannot_be_reused():
    tg = TaskGroup()
    with tg:
        pass
    with pytest.raises(RuntimeError, match="cannot be reused"):
        with tg:
            pass


# ---------------------------------------------------------------------------
# Worker reuse / shared pool
# ---------------------------------------------------------------------------


def test_workers_are_shared_across_groups(pool: ThreadPool, fast_idle_timeout):
    """A worker idle from one group should be reused by another."""
    seen: set[int] = set()

    def record():
        seen.add(threading.get_ident())

    with TaskGroup() as tg:
        for _ in range(4):
            tg.create_task(record).result(timeout=5)
    first_run = set(seen)

    # Second group sees at least some of the same threads (workers parked
    # in the pool's idle deque).
    seen.clear()
    with TaskGroup() as tg:
        for _ in range(4):
            tg.create_task(record).result(timeout=5)
    second_run = set(seen)

    assert first_run & second_run, "no worker reuse across groups"
    _drain_idle(pool)


def test_idle_workers_self_exit_on_timeout(pool: ThreadPool, fast_idle_timeout):
    # Drop any leftover workers from earlier tests — they were spawned before
    # the monkeypatch and are stuck on their original 60 s ``inbox.get``.
    # Orphaning them is safe (daemon threads, won't be reused).
    pool.idle.clear()

    # Spawn a fresh worker and grab a reference to it.
    with TaskGroup() as tg:
        tg.create_task(lambda: None).result(timeout=5)
    assert len(pool.idle) >= 1
    fresh = pool.idle[-1]  # LIFO: most recently parked

    # Wait past the idle timeout.
    time.sleep(fast_idle_timeout * 10)
    assert fresh._alive is False

    # Submitting again skips the dead tombstone and spawns fresh.
    with TaskGroup() as tg:
        tg.create_task(lambda: None).result(timeout=5)
    _drain_idle(pool)


# ---------------------------------------------------------------------------
# Race / stress
# ---------------------------------------------------------------------------


def test_claim_vs_idle_timeout_race(pool: ThreadPool, fast_idle_timeout):
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
        with TaskGroup() as tg:
            futs = [tg.create_task(tick) for _ in range(n)]
        for f in futs:
            f.result(timeout=5)
        # Sleep to let some workers idle-time-out, leaving tombstones.
        time.sleep(fast_idle_timeout * 1.5)

    assert counter == n * 5
    _drain_idle(pool)


def test_start_soon_from_arbitrary_thread():
    """``start_soon`` must be safe to call from any thread."""
    results: list[int] = []
    results_lock = threading.Lock()

    def producer(tg: TaskGroup):
        for i in range(10):
            tg.start_soon(_record, i, results, results_lock)

    with TaskGroup() as tg:
        threads = [threading.Thread(target=producer, args=(tg,)) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert sorted(results) == sorted(list(range(10)) * 5)


def _record(i: int, sink: list[int], lock: threading.Lock) -> None:
    with lock:
        sink.append(i)


# ---------------------------------------------------------------------------
# warmup
# ---------------------------------------------------------------------------


def test_warmup_adds_workers_to_idle_pool(pool: ThreadPool, portal: BlockingPortal):
    """``await pool.warmup(N)`` spawns N workers and parks them in the
    pool's idle deque."""
    pool.idle.clear()
    portal.call(pool.warmup, 4)
    assert len(pool.idle) == 4
    workers = list(pool.idle)
    assert all(w._alive for w in workers)
    assert len({id(w) for w in workers}) == 4


def test_warmup_workers_are_used_by_dispatch(pool: ThreadPool, portal: BlockingPortal):
    """A subsequent ``start_soon`` reuses a pre-warmed worker rather than
    spawning a fresh one."""
    pool.idle.clear()
    portal.call(pool.warmup, 2)
    pre = {id(w) for w in pool.idle}

    seen: set[int] = set()
    seen_lock = threading.Lock()

    def record_self() -> None:
        with seen_lock:
            seen.add(threading.get_ident())

    with TaskGroup() as tg:
        tg.create_task(record_self).result(timeout=5)

    # The worker that ran is one of the pre-warmed set.
    post = {id(w) for w in pool.idle}
    assert pre & post  # at least one of the warmed workers is still parked


def test_warmup_zero_is_noop(pool: ThreadPool, portal: BlockingPortal):
    pool.idle.clear()
    portal.call(pool.warmup, 0)
    assert len(pool.idle) == 0


def test_warmup_negative_raises(pool: ThreadPool, portal: BlockingPortal):
    with pytest.raises(ValueError, match=">= 0"):
        portal.call(pool.warmup, -1)


# ---------------------------------------------------------------------------
# ContextVar propagation
# ---------------------------------------------------------------------------


def test_task_sees_caller_context():
    """A ContextVar set in the caller is visible inside the task."""
    var: contextvars.ContextVar[str] = contextvars.ContextVar("test_var")
    var.set("caller-value")

    with TaskGroup() as tg:
        fut = tg.create_task(var.get)

    assert fut.result(timeout=5) == "caller-value"


def test_task_mutation_does_not_leak_to_caller():
    """ContextVar.set inside a task is confined to the task's context copy."""
    var: contextvars.ContextVar[str] = contextvars.ContextVar("test_var", default="original")

    def mutate():
        var.set("task-mutated")
        return var.get()

    with TaskGroup() as tg:
        fut = tg.create_task(mutate)

    assert fut.result(timeout=5) == "task-mutated"
    assert var.get() == "original"  # caller's view unchanged


def test_each_start_soon_captures_independently():
    """Two ``start_soon`` calls see different values when the body mutates
    the ContextVar between them — capture is per-call, not per-group."""
    var: contextvars.ContextVar[int] = contextvars.ContextVar("test_var", default=0)

    with TaskGroup() as tg:
        var.set(1)
        f1 = tg.create_task(var.get)
        var.set(2)
        f2 = tg.create_task(var.get)

    assert f1.result(timeout=5) == 1
    assert f2.result(timeout=5) == 2


def test_concurrent_tasks_see_their_own_context():
    """N concurrent tasks each captured a different ContextVar value;
    each must see its own, not another task's."""
    var: contextvars.ContextVar[int] = contextvars.ContextVar("test_var")

    barrier = threading.Barrier(10)

    def observe() -> int:
        # Sync all tasks at the same wall-clock moment so they're guaranteed
        # to be running concurrently when they read the ContextVar.
        barrier.wait(timeout=5)
        return var.get()

    with TaskGroup() as tg:
        futs: list[Future[int]] = []
        for i in range(10):
            var.set(i)
            futs.append(tg.create_task(observe))

    assert sorted(f.result(timeout=5) for f in futs) == list(range(10))


# ---------------------------------------------------------------------------
# Async callables (dispatched via the pool's portal)
# ---------------------------------------------------------------------------


def test_async_callable_runs_via_pool_portal():
    """An async callable submitted via ``create_task`` is awaited on the
    pool's portal event loop; the future resolves with the awaited result."""

    async def add(a: int, b: int) -> int:
        return a + b

    with TaskGroup() as tg:
        fut = tg.create_task(add, 2, 3)

    assert fut.result(timeout=5) == 5


def test_async_callable_exception_flows_through_future():
    """Exceptions raised inside an async callable surface on the future
    (and into the TaskGroup's exception group)."""

    async def boom():
        raise ValueError("boom")

    tg = TaskGroup()
    with pytest.raises(ExceptionGroup) as ei:
        with tg:
            fut = tg.create_task(boom)

    with pytest.raises(ValueError, match="boom"):
        fut.result(timeout=5)
    assert any(isinstance(e, ValueError) for e in ei.value.exceptions)
