import anyio
import pytest
from anyio import CancelScope

import localpost
from localpost._utils import wait_any  # noqa
from localpost.hosting import Host, ServiceLifetimeManager

pytestmark = pytest.mark.anyio


async def test_host_status():
    async def test_service(lifetime):
        shutdown_scope = CancelScope()
        lifetime.set_started(graceful_shutdown_scope=shutdown_scope)
        with shutdown_scope:
            await anyio.sleep_forever()

    host = Host(test_service)
    assert not host.started
    async with host.aserve():
        await host.started
        assert host.status  # TODO Check
        host.shutdown()


async def test_exit_code_on_error():
    async def failing_service(_):
        raise Exception("Test error")

    host = Host(failing_service)
    async with host.aserve():
        pass

    assert host.exit_code == 1


async def test_shutdown():
    shutdown_communicated = False
    shutdown_reason = "Test shutdown"

    async def test_service(lifetime: ServiceLifetimeManager) -> None:
        lifetime.set_started()
        await wait_any(anyio.sleep_forever, lifetime.shutting_down)
        nonlocal shutdown_communicated
        shutdown_communicated = True

    host = Host(test_service)
    async with host.aserve():
        await host.started.wait()
        assert not host.shutting_down
        host.shutdown(reason=shutdown_reason)
        assert host.shutting_down

    assert shutdown_communicated  # Shutdown was communicated to the service
    assert host.status  # TODO Check


async def test_arun():
    async def a_service(_: ServiceLifetimeManager):
        await anyio.sleep(0.1)

    with localpost.debug:
        host = Host(a_service)
        exit_code = await localpost.arun(host)

    assert exit_code == 0


def test_run():
    async def a_service(_: ServiceLifetimeManager):
        await anyio.sleep(0.1)

    with localpost.debug:
        host = Host(a_service)
        exit_code = localpost.run(host)
    assert exit_code == 0
