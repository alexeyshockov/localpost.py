import anyio
import pytest
from anyio import WouldBlock, create_memory_object_stream, create_task_group, sleep

from localpost import flow
from localpost.consumers.stream import stream_consumer
from localpost.flow import batch
from localpost.hosting import Host

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@pytest.fixture()
async def local_queue():
    writer, reader = create_memory_object_stream(10)
    yield writer, reader


async def test_normal_case(local_queue):
    writer, reader = local_queue

    # Arrange

    sent = ["hello", "world", "!"]
    for message in sent:
        writer.send_nowait(message)

    # Act

    received = []

    @stream_consumer(reader)
    @flow.handler
    async def handle(m: str):
        nonlocal received
        received += [m]

    host = Host(handle)
    async with host.aserve():
        await anyio.sleep(0.5)  # "App is working"
        writer.close()

    # Assert

    assert host.status["exception"] is None
    assert received == sent


async def test_batching(local_queue):
    writer, reader = local_queue

    # TODO Implement


async def test_buffering(local_queue):
    writer, reader = local_queue

    # TODO Implement
