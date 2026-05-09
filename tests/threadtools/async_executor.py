"""Tests for the AnyIO-backed executors.

Both :class:`AsyncWorkerExecutor` and :class:`AsyncExecutor` are async
context managers and require a caller-owned
:class:`anyio.from_thread.BlockingPortal` running on the test's loop.

We pin both backends (``asyncio`` and ``trio``) for the cross-backend
``check_cancelled`` story; ``stop()`` must work the same on each.
"""

from __future__ import annotations

import threading
import time
from collections.abc import AsyncIterator
from concurrent.futures import Future

import anyio
import anyio.from_thread
import pytest
from anyio.from_thread import BlockingPortal

from localpost.threadtools import AsyncExecutor, AsyncWorkerExecutor

# Both backends, on every test in this module.
pytestmark = pytest.mark.parametrize("anyio_backend", ["asyncio", "trio"])


@pytest.fixture
async def portal() -> AsyncIterator[BlockingPortal]:
    """A :class:`BlockingPortal` bound to the test's running event loop.

    Submits to async executors are sync calls that schedule onto the
    portal's loop; they must be invoked from a non-loop thread (the test
    body uses ``to_thread.run_sync`` to do that).
    """
    async with anyio.from_thread.BlockingPortal() as p:
        yield p


# ---------------------------------------------------------------------------
# AsyncWorkerExecutor
# ---------------------------------------------------------------------------


async def test_async_worker_executor_basic_submit(anyio_backend, portal: BlockingPortal):
    async with AsyncWorkerExecutor(portal=portal) as ex:
        # ``submit`` and ``Future.result`` must both run off the loop thread.
        fut: Future[int] = await anyio.to_thread.run_sync(ex.submit, lambda: 42)
        result = await anyio.to_thread.run_sync(fut.result, 5)
        assert result == 42


async def test_async_worker_executor_check_cancelled_callable_in_task(anyio_backend, portal: BlockingPortal):
    """``from_thread.check_cancelled`` is callable inside a task running on an
    :class:`AsyncWorkerExecutor` worker — i.e. AnyIO's threadlocals are wired up.
    """
    polled = threading.Event()

    def task() -> str:
        anyio.from_thread.check_cancelled()  # raises if not wired
        polled.set()
        return "ok"

    async with AsyncWorkerExecutor(portal=portal) as ex:
        fut = await anyio.to_thread.run_sync(ex.submit, task)
        result = await anyio.to_thread.run_sync(fut.result, 5)
        assert result == "ok"
    assert polled.is_set()


async def test_async_worker_executor_stop_propagates_to_running_task(anyio_backend, portal: BlockingPortal):
    """:meth:`stop` cancels the internal task group; running tasks polling
    ``check_cancelled`` raise on their next checkpoint."""
    saw_cancel = threading.Event()
    started = threading.Event()

    def slow_task() -> None:
        started.set()
        try:
            for _ in range(500):
                anyio.from_thread.check_cancelled()
                time.sleep(0.005)
        except BaseException:
            saw_cancel.set()
            raise

    async with AsyncWorkerExecutor(portal=portal) as ex:
        fut = await anyio.to_thread.run_sync(ex.submit, slow_task)
        await anyio.to_thread.run_sync(started.wait, 2.0)
        await anyio.to_thread.run_sync(ex.stop)
        # __aexit__ awaits the cancellation-driven drain.

    assert saw_cancel.is_set()
    # The future ends up with a cancellation exception (backend-specific type).
    assert fut.done()
    assert fut.exception() is not None


# ---------------------------------------------------------------------------
# AsyncExecutor
# ---------------------------------------------------------------------------


async def test_async_executor_basic_submit(anyio_backend, portal: BlockingPortal):
    async with AsyncExecutor(portal=portal) as ex:
        fut: Future[int] = await anyio.to_thread.run_sync(ex.submit, lambda: 7 * 6)
        result = await anyio.to_thread.run_sync(fut.result, 5)
        assert result == 42


async def test_async_executor_propagates_exception(anyio_backend, portal: BlockingPortal):
    class Boom(Exception):
        pass

    def bad() -> None:
        raise Boom("nope")

    async with AsyncExecutor(portal=portal) as ex:
        fut = await anyio.to_thread.run_sync(ex.submit, bad)

        def get_result() -> object:
            return fut.result(timeout=5)

        with pytest.raises(Boom):
            await anyio.to_thread.run_sync(get_result)


async def test_async_executor_capacity_limiter(anyio_backend, portal: BlockingPortal):
    """``max_concurrency`` is enforced via :class:`anyio.CapacityLimiter`."""
    n_concurrent = 0
    max_seen = 0
    lock = threading.Lock()

    def hold() -> None:
        nonlocal n_concurrent, max_seen
        with lock:
            n_concurrent += 1
            max_seen = max(max_seen, n_concurrent)
        time.sleep(0.05)
        with lock:
            n_concurrent -= 1

    async with AsyncExecutor(portal=portal, max_concurrency=2) as ex:
        futs = [await anyio.to_thread.run_sync(ex.submit, hold) for _ in range(8)]
        for f in futs:
            await anyio.to_thread.run_sync(f.result, 5)
    assert max_seen <= 2


async def test_async_executor_stop_cancels_all_inflight(anyio_backend, portal: BlockingPortal):
    cancelled_count = 0
    cancelled_lock = threading.Lock()
    started = threading.Event()
    n = 4

    def slow_task() -> None:
        nonlocal cancelled_count
        started.set()
        try:
            for _ in range(500):
                anyio.from_thread.check_cancelled()
                time.sleep(0.005)
        except BaseException:
            with cancelled_lock:
                cancelled_count += 1
            raise

    async with AsyncExecutor(portal=portal, max_concurrency=n) as ex:
        for _ in range(n):
            await anyio.to_thread.run_sync(ex.submit, slow_task)
        await anyio.to_thread.run_sync(started.wait, 2.0)
        await anyio.to_thread.run_sync(ex.stop)

    assert cancelled_count == n
