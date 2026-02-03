import pytest

pytestmark = pytest.mark.anyio


async def test_shutdown_timeout():
    # HostedService(service_func).use(shutdown_timeout(5))
    pass  # TODO Implement


async def test_start_timeout():
    # HostedService(service_func).use(start_timeout(5))
    pass  # TODO Implement


async def test_lifespan():
    # HostedService(service_func).use(lifespan(...))
    pass  # TODO Implement
