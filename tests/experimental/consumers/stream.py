import pytest
from anyio import create_memory_object_stream, sleep

from localpost.experimental.consumers.stream import stream_consumer
from localpost.hosting import serve

pytestmark = [pytest.mark.anyio]


@pytest.fixture()
def local_queue():
    writer, reader = create_memory_object_stream(10)
    return writer, reader


async def test_normal_case(local_queue):
    writer, reader = local_queue

    # Arrange
    sent = ["hello", "world", "!"]
    for message in sent:
        writer.send_nowait(message)

    # Act
    received = []

    async def handle(m: str):
        received.append(m)

    consumer = stream_consumer(reader, handle)

    async with serve(consumer) as lt:
        await sleep(0.5)
        writer.close()
        await lt.stopped

    # Assert
    assert received == sent
