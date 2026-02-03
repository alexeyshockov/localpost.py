import random
import string

import anyio
import boto3
import pytest
from testcontainers.localstack import LocalStackContainer
from types_boto3_sqs.client import SQSClient

from localpost import flow, debug
from localpost.consumers.sqs import SqsMessage, SqsMessages, sqs_queue_consumer, ConsumerClient
from localpost.hosting import Host

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@pytest.fixture(scope="module")
def local_sqs():
    with LocalStackContainer("localstack/localstack:4.0").with_services("sqs") as aws:
        aws_url = aws.get_url()

        conn_params = {
            "endpoint_url": aws_url,
            "region_name": aws.region_name,
            "aws_access_key_id": "test",
            "aws_secret_access_key": "test",
        }

        yield conn_params


@pytest.fixture()
def queue_name():
    return "test_" + "".join(random.choices(string.ascii_uppercase + string.digits, k=10))


async def test_happy_path(local_sqs, queue_name):
    # Arrange

    sent = ["hello", "world", "!"]
    sqs_client: SQSClient = boto3.client("sqs", **local_sqs)
    queue_url = sqs_client.create_queue(
        QueueName=queue_name, Attributes={"VisibilityTimeout": "1"}
    )["QueueUrl"]
    for message in sent:
        sqs_client.send_message(QueueUrl=queue_url, MessageBody=message)

    # Act

    received = []

    @sqs_queue_consumer(lambda: ConsumerClient.create(queue_url, sqs_client))
    @flow.handler
    async def handle(m: SqsMessage):
        nonlocal received
        with m as m_body:
            received += [m_body]

    host = Host(handle)
    async with debug, host.aserve():
        await anyio.sleep(3)  # "App is working"
        host.shutdown()

    # Assert

    assert host.status["exception"] is None
    assert received == sent

    # Make sure that the messages were deleted from the queue
    queue_info = sqs_client.get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"])
    assert int(queue_info["Attributes"]["ApproximateNumberOfMessages"]) == 0
    assert int(queue_info["Attributes"]["ApproximateNumberOfMessagesNotVisible"]) == 0


async def test_handler_manager():
    # Test handler lifecycle (via handler manager)
    pass


async def test_batching(local_sqs, queue_name):
    # Arrange

    sent = ["hello", "world", "!"]
    sqs_client: SQSClient = boto3.client("sqs", **local_sqs)
    queue_url = sqs_client.create_queue(
        QueueName=queue_name, Attributes={"VisibilityTimeout": "2"}  # Should be greater than the batch window
    )["QueueUrl"]
    for message in sent:
        sqs_client.send_message(QueueUrl=queue_url, MessageBody=message)

    # Act

    received = []

    @sqs_queue_consumer(lambda: ConsumerClient.create(queue_url, sqs_client))
    @flow.batch(10, 1, SqsMessages)
    @flow.handler
    async def handle(messages: SqsMessages):
        nonlocal received
        with messages:
            received += [[m.body for m in messages]]

    host = Host(handle)
    async with debug, host.aserve():
        await anyio.sleep(3)  # "App is working"
        host.shutdown()

    # Assert

    assert host.status["exception"] is None
    assert received == [sent]

    # Make sure that the messages were deleted from the queue
    queue_info = sqs_client.get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"])
    assert int(queue_info["Attributes"]["ApproximateNumberOfMessages"]) == 0
    assert int(queue_info["Attributes"]["ApproximateNumberOfMessagesNotVisible"]) == 0
