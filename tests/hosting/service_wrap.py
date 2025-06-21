import pytest

pytestmark = pytest.mark.anyio


async def test_wrap_service():
    # Like HostedService(service_func1) >> service_func2
    pass  # TODO Implement


async def test_wrap_multiple_services():
    # Like HostedService(service_func1) >> [service_func2, service_func3]
    pass  # TODO Implement


async def test_wrap_empty_set():
    # Like HostedService(service_func1) >> []
    pass  # TODO Implement
