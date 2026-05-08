"""Tests for ``localpost.threadtools.thread_pool`` and cross-backend
``from_thread.check_cancelled`` support inside worker threads.

These tests explicitly run on both asyncio and trio. The point of the
pool's design is that workers are spawned via
``anyio.to_thread.run_sync`` so AnyIO populates the thread-local state
needed for ``from_thread.check_cancelled`` to work — and that should be
backend-agnostic.
"""

from __future__ import annotations

import threading
import time

import anyio
import anyio.from_thread
import pytest

from localpost.threadtools import TaskGroup, thread_pool
from localpost.threadtools._task_group import _current_pool


# Both backends, on every test in this module. Each test manages its
# own pool lifecycle (``async with thread_pool():``), so opt out of the
# autouse ambient pool from ``conftest.py``.
pytestmark = [
    pytest.mark.parametrize("anyio_backend", ["asyncio", "trio"]),
    pytest.mark.no_ambient_pool,
]


async def test_check_cancelled_raises_in_worker_after_outer_cancel(anyio_backend):
    """A worker task polling ``from_thread.check_cancelled`` observes
    cancellation when an outer scope wrapping ``thread_pool()`` cancels.

    This is the cross-backend behaviour the pool exists to provide:
    workers are spawned via ``to_thread.run_sync(abandon_on_cancel=False)``,
    which sets up the threadlocals that AnyIO's ``check_cancelled``
    reads. Cancelling any ancestor scope of the pool's host task group
    propagates to all in-flight worker tasks.
    """
    saw_cancel = threading.Event()
    runner_done = threading.Event()
    runner_exc: list[BaseException] = []

    def slow_task():
        try:
            for _ in range(500):
                anyio.from_thread.check_cancelled()
                time.sleep(0.01)
        except BaseException:
            saw_cancel.set()
            raise

    def runner(pool):
        # ``threading.Thread`` doesn't propagate contextvars; set the
        # pool explicitly so ``TaskGroup()`` here finds it.
        _current_pool.set(pool)
        try:
            with TaskGroup() as tg:
                tg.start_soon(slow_task)
        except BaseException as e:  # noqa: BLE001 — capturing for assertion
            runner_exc.append(e)
        finally:
            runner_done.set()

    # Wrap ``thread_pool`` in an outer cancellable scope so we can
    # cancel it without conflating teardown semantics with cancellation
    # propagation.
    with anyio.CancelScope() as scope:
        async with thread_pool() as pool:
            runner_thread = threading.Thread(target=runner, args=(pool,), daemon=True)
            runner_thread.start()
            # Let the worker get scheduled and start polling.
            await anyio.sleep(0.1)
            scope.cancel()
            # ``thread_pool.__aexit__`` will run as the ``async with``
            # unwinds under cancellation; in-flight ``check_cancelled``
            # calls raise.

    runner_thread.join(timeout=5)
    assert runner_done.is_set(), "runner thread did not terminate"
    assert saw_cancel.is_set(), "worker task did not observe cancellation"
    assert runner_exc, "TaskGroup did not surface an exception"
    assert isinstance(runner_exc[0], BaseExceptionGroup)


async def test_warmup_populates_idle(anyio_backend):
    """``await pool.warmup(N)`` spawns N workers and parks them in
    ``pool.idle`` on both backends."""
    async with thread_pool() as pool:
        await pool.warmup(3)
        assert len(pool.idle) == 3
        assert all(w._alive for w in pool.idle)


async def test_pool_teardown_drains_idle_workers(anyio_backend):
    """Idle workers parked in ``_cv.wait`` must exit promptly when the
    pool is torn down — otherwise teardown blocks for the full
    ``IDLE_TIMEOUT`` (60 s)."""
    started = time.monotonic()
    async with thread_pool() as pool:
        await pool.warmup(2)
    elapsed = time.monotonic() - started
    # Generous bound: shutdown should take well under a second; the
    # 60 s ``IDLE_TIMEOUT`` would catastrophically blow this.
    assert elapsed < 5.0, f"pool teardown took {elapsed:.1f}s — idle workers not woken"


async def test_taskgroup_outside_pool_raises(anyio_backend):
    """The error message should point users at ``thread_pool`` /
    ``run_app`` rather than failing silently or with a confusing
    AttributeError."""
    # Make sure no ambient pool from a prior test leaks in.
    assert _current_pool.get(None) is None
    with pytest.raises(RuntimeError, match="thread_pool"):
        TaskGroup()
