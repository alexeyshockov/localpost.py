import pytest

from localpost.hosting import HostedService, hosted_service

pytestmark = pytest.mark.anyio


async def test_decorator():
    @hosted_service
    def simple_sync_service():
        pass

    assert isinstance(simple_sync_service, HostedService)


# TODO Test attributes (name especially, separately)
