"""Tests for :class:`localpost.threadtools.WorkerExecutor` (sync, no AnyIO).

The Async variants (``AsyncWorkerExecutor`` / ``AsyncExecutor``) live in
``async_executor.py`` since they're async context managers and need a
:class:`anyio.from_thread.BlockingPortal`.
"""

from __future__ import annotations

import contextvars
import math
import threading
import time

import pytest

from localpost.threadtools import WorkerExecutor
from localpost.threadtools import _executor as _ex


@pytest.fixture
def fast_idle_timeout(monkeypatch: pytest.MonkeyPatch) -> float:
    """Make idle-timeout tests fast: 100 ms instead of 60 s."""
    monkeypatch.setattr(_ex, "DEFAULT_IDLE_TIMEOUT", 0.1)
    return 0.1


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
# Lifecycle / lazy spawn / max_concurrency
# ---------------------------------------------------------------------------


def test_lazy_worker_spawn_under_max_concurrency():
    """Workers are spawned on demand — never more than ``max_concurrency``."""
    seen: set[int] = set()
    seen_lock = threading.Lock()

    def record():
        with seen_lock:
            seen.add(threading.get_ident())
        time.sleep(0.005)

    with WorkerExecutor(max_concurrency=3) as ex:
        futs = [ex.submit(record) for _ in range(40)]
        for f in futs:
            f.result(timeout=5)
    assert len(seen) <= 3


def test_unlimited_concurrency_spawns_per_pending_task():
    """Default (math.inf) → every concurrent submission gets its own worker thread."""
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
        # Default max_concurrency is math.inf.
        assert math.isinf(ex._max_concurrency)
        futs = [ex.submit(hold) for _ in range(n)]
        for f in futs:
            f.result(timeout=5)
    assert len(seen) == n


def test_idle_workers_self_exit_on_timeout(fast_idle_timeout: float):
    """A worker that sees no work for ``idle_timeout`` should retire."""
    with WorkerExecutor(idle_timeout=fast_idle_timeout) as ex:
        ex.submit(lambda: None).result(timeout=5)
        time.sleep(fast_idle_timeout * 5)
        # Workers self-exit and remove themselves from open_receivers.
        assert ex.open_receivers == []
        # Submitting again still works — a fresh worker spawns.
        ex.submit(lambda: None).result(timeout=5)


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


def test_max_concurrency_must_be_positive():
    with pytest.raises(ValueError, match="max_concurrency"):
        WorkerExecutor(max_concurrency=0)


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
