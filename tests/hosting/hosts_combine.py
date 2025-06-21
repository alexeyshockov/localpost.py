import pytest
from anyio import sleep

from localpost.hosting import Host, HostedService

pytestmark = pytest.mark.anyio


async def test_combine():
    async def service1(lifetime):
        lifetime.set_started()
        await lifetime.shutting_down.wait()

    async def service2(lifetime):
        lifetime.set_started()
        await lifetime.shutting_down.wait()

    async def service3(lifetime):
        lifetime.set_started()
        await lifetime.shutting_down.wait()

    combined = HostedService(service1) + service2 + service3
    assert isinstance(combined, HostedService)

    host = Host(combined)
    assert not host.started
    async with host.aserve():
        await host.started
        await sleep(0.1)
        assert host.status  # TODO Check
        host.shutdown()
