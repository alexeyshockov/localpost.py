import pytest

from localpost import flow
from localpost.flow import FlowHandlerManager

pytestmark = pytest.mark.anyio


async def test_handler_decorator():
    handled_items = []

    @flow.handler
    def sample_handler(item):
        handled_items.append(item)

    assert isinstance(sample_handler, FlowHandlerManager)

    async with sample_handler as handler:
        handler("sample item")

    assert handled_items == ["sample item"]


async def test_handler_manager_decorator():
    handled_items = []

    @flow.handler_manager
    async def sample_handler_manager():
        def sample_handler(item):
            handled_items.append(item)

        yield sample_handler

    assert isinstance(sample_handler_manager, FlowHandlerManager)

    async with sample_handler_manager as handler:
        handler("sample item")

    assert handled_items == ["sample item"]



# TODO Test ensure_async_handler
# TODO Test ensure_async_handler_manager

# TODO Test ensure_sync_handler
# TODO Test ensure_sync_handler_manager
