"""Tests for :class:`localpost.threadtools.AsyncWorkerExecutor`.

It's an async context manager and requires a caller-owned
:class:`localpost.Portal` running on the test's loop.

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
import anyio.to_thread
import pytest

from localpost import Portal
from localpost.threadtools import AsyncWorkerExecutor

# Both backends, on every test in this module.
pytestmark = pytest.mark.parametrize("anyio_backend", ["asyncio", "trio"])


@pytest.fixture
async def portal() -> AsyncIterator[Portal]:
    """A :class:`Portal` bound to the test's running event loop.

    Submits to async executors are sync calls that schedule onto the
    portal's loop. With :class:`Portal`, calls are safe from any thread —
    on-loop they're direct, off-loop they hop through the underlying
    :class:`BlockingPortal`.
    """
    async with anyio.from_thread.BlockingPortal() as raw:
        yield Portal(raw)


async def test_async_worker_executor_basic_submit(anyio_backend, portal: Portal):
    async with AsyncWorkerExecutor(portal=portal) as ex:
        # ``submit`` and ``Future.result`` must both run off the loop thread.
        fut: Future[int] = await anyio.to_thread.run_sync(ex.submit, lambda: 42)
        result = await anyio.to_thread.run_sync(fut.result, 5)
        assert result == 42


async def test_async_worker_executor_check_cancelled_callable_in_task(anyio_backend, portal: Portal):
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


async def test_async_worker_executor_stop_propagates_to_running_task(anyio_backend, portal: Portal):
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
