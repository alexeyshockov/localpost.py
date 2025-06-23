import pytest

from localpost import flow
from localpost.flow import FlowHandler, FlowHandlerManager

pytestmark = pytest.mark.anyio


async def test_custom_middleware():
    handled_items = []

    @flow.handler_middleware
    async def custom_middleware(next_handler: FlowHandler):
        def modified_handler(item):
            handled_items.append("modified " + item)

        yield next_handler.create(sync_h=modified_handler)

    @custom_middleware
    @flow.handler
    def sample_handler(item):
        handled_items.append(item)

    assert isinstance(sample_handler, FlowHandlerManager)

    async with sample_handler as handler:
        handler("sample item")

    assert handled_items == ["modified sample item"]
