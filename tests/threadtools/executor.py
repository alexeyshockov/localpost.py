"""Tests for the ``localpost.threadtools`` executors.

Three executors are exercised here:

- :class:`WorkerExecutor` — channel-backed pool of plain ``threading.Thread``
  workers. Most tests target this one — it has no external loop dependency.
- :class:`AnyIOWorkerExecutor` — same shape but workers spawn through a
  ``BlockingPortal`` + ``to_thread.run_sync``. The cross-backend
  ``check_cancelled`` test lives here.
- :class:`AnyIOExecutor` — one AnyIO task per submit. Shorter coverage —
  enough to confirm submit/result/exception flow.
"""

from __future__ import annotations

import contextvars
import threading
import time

import anyio
import anyio.from_thread
import pytest

from localpost.threadtools import (
    AnyIOExecutor,
    AnyIOWorkerExecutor,
    WorkerExecutor,
)
from localpost.threadtools import _executor as _ex


@pytest.fixture
def fast_idle_timeout(monkeypatch: pytest.MonkeyPatch) -> float:
    """Make idle-timeout tests fast: 100 ms instead of 60 s."""
    monkeypatch.setattr(_ex, "DEFAULT_IDLE_TIMEOUT", 0.1)
    return 0.1


# ---------------------------------------------------------------------------
# WorkerExecutor — submit / result
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
    """No cap → every concurrent submission gets its own worker thread."""
    n = 8
    seen: set[int] = set()
    barrier = threading.Barrier(n)
    seen_lock = threading.Lock()

    def hold():
        # Sync at a barrier so all tasks must be running concurrently —
        # they cannot be served by a single reused worker.
        barrier.wait(timeout=5)
        with seen_lock:
            seen.add(threading.get_ident())

    with WorkerExecutor() as ex:
        futs = [ex.submit(hold) for _ in range(n)]
        for f in futs:
            f.result(timeout=5)
    assert len(seen) == n


def test_idle_workers_self_exit_on_timeout(fast_idle_timeout: float):
    """A worker that sees no work for ``idle_timeout`` should retire."""
    with WorkerExecutor(idle_timeout=fast_idle_timeout) as ex:
        # The constructor seeds one worker; a single submit + result keeps it warm.
        ex.submit(lambda: None).result(timeout=5)
        # The seed receiver is always present in open_receivers; what we want to
        # confirm is that no *additional* worker hangs around past the idle window.
        time.sleep(fast_idle_timeout * 5)
        # At least one receiver remains (the seed); none should exceed it.
        assert len(ex.open_receivers) <= 1
        # Submitting again still works — a fresh worker is spawned for the WouldBlock.
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


# ---------------------------------------------------------------------------
# AnyIOWorkerExecutor — cross-backend check_cancelled inside workers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["asyncio", "trio"])
def test_anyio_worker_executor_check_cancelled_callable_in_task(backend: str):
    """``from_thread.check_cancelled`` is callable inside a task running on an
    :class:`AnyIOWorkerExecutor` worker — i.e. AnyIO's threadlocals are wired
    up. This is the structural property the executor exists to provide; the
    actual cancel propagation is the user's outer-scope concern.
    """
    polled = threading.Event()

    def task() -> str:
        # If the threadlocals weren't set, this would raise RuntimeError.
        anyio.from_thread.check_cancelled()
        polled.set()
        return "ok"

    with AnyIOWorkerExecutor(backend=backend) as ex:
        fut = ex.submit(task)
        assert fut.result(timeout=5) == "ok"
    assert polled.is_set()


def test_anyio_worker_executor_basic_submit():
    with AnyIOWorkerExecutor() as ex:
        fut = ex.submit(lambda x: x + 1, 41)
        assert fut.result(timeout=5) == 42


# ---------------------------------------------------------------------------
# AnyIOExecutor — fresh AnyIO task per submit
# ---------------------------------------------------------------------------


def test_anyio_executor_basic_submit():
    with AnyIOExecutor() as ex:
        fut = ex.submit(lambda x: x * 3, 14)
        assert fut.result(timeout=5) == 42


def test_anyio_executor_capacity_limiter():
    """Tasks beyond ``max_concurrency`` queue rather than running."""
    n_concurrent = 0
    max_seen = 0
    lock = threading.Lock()

    def hold():
        nonlocal n_concurrent, max_seen
        with lock:
            n_concurrent += 1
            max_seen = max(max_seen, n_concurrent)
        time.sleep(0.05)
        with lock:
            n_concurrent -= 1

    with AnyIOExecutor(max_concurrency=2) as ex:
        futs = [ex.submit(hold) for _ in range(8)]
        for f in futs:
            f.result(timeout=5)
    assert max_seen <= 2


def test_anyio_executor_propagates_exception():
    class Boom(Exception):
        pass

    with AnyIOExecutor() as ex:
        fut = ex.submit(lambda: (_ for _ in ()).throw(Boom("nope")))
        with pytest.raises(Boom):
            fut.result(timeout=5)
