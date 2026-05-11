import anyio
import pytest

from localpost.hosting import ServiceLifetime, serve
from localpost.hosting.middleware import start_timeout

pytestmark = pytest.mark.anyio


async def test_start_timeout_ok():
    @start_timeout(1.0)
    async def fast_starter(lt: ServiceLifetime):
        lt.set_started()
        await lt.shutting_down.wait()

    async with serve(fast_starter) as lt:
        await lt.started
        lt.shutdown()
        await lt.stopped

    assert lt.exit_code == 0


async def test_start_timeout_exceeded():
    @start_timeout(0.1)
    async def slow_starter(lt: ServiceLifetime):
        await anyio.sleep(10)  # Never calls set_started()

    async with serve(slow_starter) as lt:
        await lt.stopped

    assert lt.exit_code == 1
