import pytest

from localpost import flow
from localpost.flow import FlowHandler, FlowHandlerManager

pytestmark = pytest.mark.anyio


async def test_custom_middleware():
    handled_items = []

    @flow.handler_middleware
    async def custom_middleware(next_handler: FlowHandler):
        async def modified_async_handler(item):
            handled_items.append("modified async " + item)
            await next_handler.async_h(item)

        def modified_sync_handler(item):
            handled_items.append("modified sync " + item)
            next_handler.sync_h(item)

        yield modified_async_handler, modified_sync_handler

    @custom_middleware
    @flow.handler
    def sample_handler(item):
        handled_items.append(item)

    assert isinstance(sample_handler, FlowHandlerManager)

    async with sample_handler as handler:
        handler("sample item")

    assert len(handled_items) == 2
    assert "modified sync sample item" in handled_items
    assert "sample item" in handled_items
