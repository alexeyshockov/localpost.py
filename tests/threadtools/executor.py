"""Tests for :class:`localpost.threadtools.WorkerExecutor` (sync, no AnyIO).

The Async variant (``AsyncWorkerExecutor``) lives in ``async_executor.py``
since it's an async context manager and needs a :class:`localpost.Portal`.
"""

from __future__ import annotations

import contextvars
import threading
import time

import pytest

from localpost.threadtools import WorkerExecutor

# ---------------------------------------------------------------------------
# Submit / result
# ---------------------------------------------------------------------------


def test_submit_returns_future_with_result(executor: WorkerExecutor):
    fut = executor.submit(lambda x: x * 2, 21)
    assert fut.result(timeout=5) == 42


def test_many_concurrent_submissions_all_complete(executor: WorkerExecutor):
    n = 50

    def work(i: int) -> int:
        time.sleep(0.005)
        return i * i

    futs = [executor.submit(work, i) for i in range(n)]
    assert sorted(f.result() for f in futs) == [i * i for i in range(n)]


def test_submit_with_kwargs(executor: WorkerExecutor):
    fut = executor.submit(lambda *, a, b: a + b, a=1, b=2)
    assert fut.result(timeout=5) == 3


def test_submit_propagates_exception_to_future(executor: WorkerExecutor):
    class Boom(Exception):
        pass

    def bad():
        raise Boom("nope")

    fut = executor.submit(bad)
    with pytest.raises(Boom):
        fut.result(timeout=5)


# ---------------------------------------------------------------------------
# Lifecycle / spawn-on-demand
# ---------------------------------------------------------------------------


def test_spawn_on_demand_per_pending_task():
    """Concurrent submissions with no idle worker each get a fresh worker."""
    n = 8
    seen: set[int] = set()
    barrier = threading.Barrier(n)
    seen_lock = threading.Lock()

    def hold():
        # Sync at a barrier so all tasks must be running concurrently.
        barrier.wait(timeout=5)
        with seen_lock:
            seen.add(threading.get_ident())

    with WorkerExecutor() as ex:
        futs = [ex.submit(hold) for _ in range(n)]
        for f in futs:
            f.result(timeout=5)
    assert len(seen) == n


def test_idle_workers_are_reused():
    """Workers stay around when idle and pick up subsequent submissions."""
    with WorkerExecutor() as ex:
        ex.submit(lambda: None).result(timeout=5)
        spawned = list(ex.workers)
        assert len(spawned) == 1
        # Idle stretch — workers must still be alive (no idle-timeout self-exit).
        time.sleep(0.05)
        assert ex.workers == spawned
        assert all(t.is_alive() for t in spawned)
        # Sequential submits reuse the single idle worker.
        for _ in range(20):
            ex.submit(lambda: None).result(timeout=5)
        assert len(ex.workers) == 1


def test_workers_persist_until_close():
    """Workers live for the executor's lifetime; once spawned they don't self-exit."""
    with WorkerExecutor() as ex:
        ex.submit(lambda: None).result(timeout=5)
        spawned = list(ex.workers)
        assert len(spawned) >= 1
        time.sleep(0.2)
        assert ex.workers == spawned
        assert all(t.is_alive() for t in spawned)


def test_submit_after_close_raises():
    ex = WorkerExecutor()
    with ex:
        ex.submit(lambda: None).result(timeout=5)
    with pytest.raises(RuntimeError):
        ex.submit(lambda: None)


def test_executor_cannot_be_reused():
    ex = WorkerExecutor()
    with ex:
        pass
    with pytest.raises(RuntimeError, match="cannot be reused"):
        with ex:
            pass


# ---------------------------------------------------------------------------
# ContextVar propagation
# ---------------------------------------------------------------------------


def test_task_sees_caller_context(executor: WorkerExecutor):
    var: contextvars.ContextVar[str] = contextvars.ContextVar("tt_var")
    var.set("caller-value")
    fut = executor.submit(var.get)
    assert fut.result(timeout=5) == "caller-value"


def test_task_mutation_does_not_leak_to_caller(executor: WorkerExecutor):
    var: contextvars.ContextVar[str] = contextvars.ContextVar("tt_var", default="original")

    def mutate() -> str:
        var.set("task-mutated")
        return var.get()

    fut = executor.submit(mutate)
    assert fut.result(timeout=5) == "task-mutated"
    assert var.get() == "original"


def test_each_submit_captures_independently(executor: WorkerExecutor):
    var: contextvars.ContextVar[int] = contextvars.ContextVar("tt_var", default=0)
    var.set(1)
    f1 = executor.submit(var.get)
    var.set(2)
    f2 = executor.submit(var.get)
    assert f1.result(timeout=5) == 1
    assert f2.result(timeout=5) == 2
