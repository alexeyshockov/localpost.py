import random
import string

import anyio
import pytest
from testcontainers.google import PubSubContainer

from localpost import flow, debug
from localpost.consumers.pubsub import pubsub_consumer, ConsumerClient, PubSubMessage, PubSubMessages
from localpost.hosting import Host

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@pytest.fixture(scope="module")
def local_pubsub():
    with PubSubContainer() as pubsub:
        yield pubsub


@pytest.fixture()
def topic_name():
    return "test_" + "".join(random.choices(string.ascii_uppercase + string.digits, k=10))


async def test_happy_path(local_pubsub, topic_name):
    # Arrange

    sent = ["hello", "world", "!"]
    publisher = local_pubsub.get_publisher_client()
    topic_path = publisher.topic_path(local_pubsub.project, topic_name)
    # TODO VisibilityTimeout=1
    publisher.create_topic(name=topic_path)
    for message in sent:
        publisher.publish(topic_path, message.encode("utf-8"))

    # Act

    received = []

    s_client = local_pubsub.get_subscriber_client()
    s_client.subscription_path()

    @pubsub_consumer(lambda: ConsumerClient.create(topic_path, local_pubsub.get_subscriber_client()))
    @flow.handler
    async def handle(m: PubSubMessage):
        nonlocal received
        with m as m_body:
            received += [m_body.decode()]

    host = Host(handle)
    async with debug, host.aserve():
        await anyio.sleep(3)  # "App is working"
        host.shutdown()

    # Assert

    assert host.status["exception"] is None
    assert received == sent

    # TODO Make sure that the messages were deleted from the queue


async def test_handler_manager():
    # Test handler lifecycle (via handler manager)
    pass


async def test_batching(local_pubsub, topic_name):
    # Arrange

    sent = ["hello", "world", "!"]
    publisher = local_pubsub.get_publisher_client()
    topic_path = publisher.topic_path(local_pubsub.project, topic_name)
    # TODO Attributes={"VisibilityTimeout": "2"}  # Should be greater than the batch window
    publisher.create_topic(name=topic_path)
    for message in sent:
        publisher.publish(topic_path, message.encode("utf-8"))

    # Act

    received = []

    @pubsub_consumer(lambda: ConsumerClient.create(topic_path, local_pubsub.get_subscriber_client()))
    @flow.batch(10, 1, PubSubMessages)
    @flow.handler
    async def handle(messages: PubSubMessages):
        nonlocal received
        with messages:
            received += [[m.data.decode() for m in messages]]

    host = Host(handle)
    async with debug, host.aserve():
        await anyio.sleep(3)  # "App is working"
        host.shutdown()

    # Assert

    assert host.status["exception"] is None
    assert received == [sent]

    # TODO Make sure that the messages were deleted from the queue
