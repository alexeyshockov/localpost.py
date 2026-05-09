"""Tests for :func:`localpost.threadtools.run_async`.

``run_async`` is the sync→async bridge: from a worker thread it dispatches a
coroutine onto the current service's loop and blocks for the result. The
portal it uses is resolved via :func:`localpost.hosting.current_service`, so
each test runs inside a hosted service.
"""

from __future__ import annotations

import threading

import anyio
import pytest

from localpost.hosting import ServiceLifetime, serve
from localpost.threadtools import run_async

pytestmark = pytest.mark.anyio


async def test_run_async_returns_result_from_worker_thread():
    """A worker thread dispatches an async function back onto the loop and
    receives its return value."""
    seen: dict = {}

    async def add(a: int, b: int) -> int:
        seen["loop_thread"] = threading.get_ident()
        return a + b

    def worker() -> int:
        seen["worker_thread"] = threading.get_ident()
        return run_async(add, 2, 3)

    async def svc(lt: ServiceLifetime) -> None:
        lt.set_started()
        seen["result"] = await anyio.to_thread.run_sync(worker)

    async with serve(svc) as lt:
        await lt.stopped

    assert seen["result"] == 5
    assert seen["worker_thread"] != seen["loop_thread"]


async def test_run_async_propagates_exception():
    """Exceptions raised by the coroutine surface in the calling thread."""

    class Boom(Exception):
        pass

    async def explode() -> None:
        raise Boom("nope")

    captured: dict = {}

    def worker() -> None:
        try:
            run_async(explode)
        except Boom as e:
            captured["exc"] = e

    async def svc(lt: ServiceLifetime) -> None:
        lt.set_started()
        await anyio.to_thread.run_sync(worker)

    async with serve(svc) as lt:
        await lt.stopped

    assert isinstance(captured.get("exc"), Boom)


async def test_run_async_supports_keyword_arguments():
    """``run_async`` forwards keyword arguments via :class:`functools.partial`."""

    async def greet(name: str, *, greeting: str) -> str:
        return f"{greeting}, {name}!"

    seen: dict = {}

    def worker() -> None:
        seen["result"] = run_async(greet, "world", greeting="hi")

    async def svc(lt: ServiceLifetime) -> None:
        lt.set_started()
        await anyio.to_thread.run_sync(worker)

    async with serve(svc) as lt:
        await lt.stopped

    assert seen["result"] == "hi, world!"


async def test_run_async_from_loop_thread_raises():
    """Calling :func:`run_async` from the loop thread is a deadlock; the
    underlying portal raises ``RuntimeError``."""

    async def noop() -> None:
        return None

    seen: dict = {}

    async def svc(lt: ServiceLifetime) -> None:
        lt.set_started()
        try:
            run_async(noop)
        except RuntimeError as e:
            seen["exc"] = e

    async with serve(svc) as lt:
        await lt.stopped

    assert isinstance(seen.get("exc"), RuntimeError)


def test_run_async_outside_hosting_raises():
    """No hosting context → :func:`current_service` raises ``RuntimeError``."""

    async def noop() -> None:
        return None

    with pytest.raises(RuntimeError, match="Not in hosting context"):
        run_async(noop)
