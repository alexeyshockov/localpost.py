import random
import string
from collections.abc import Collection

import anyio
import boto3
import pytest
from aiobotocore.session import get_session
from testcontainers.localstack import LocalStackContainer

from localpost import flow, flow_ops
from localpost.consumers.sqs import SqsBroker, SqsMessage
from localpost.hosting import Host

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


# See https://anyio.readthedocs.io/en/stable/testing.html#specifying-the-backends-to-run-on
@pytest.fixture
def anyio_backend():
    """
    SQS consumer uses aioboto3 which is asyncio only.
    """
    return 'asyncio'


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


async def test_normal_case(local_sqs):
    queue_name = "test_" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))

    # Arrange

    sent = ["hello", "world", "!"]
    sqs_queue = boto3.resource("sqs", **local_sqs).create_queue(QueueName=queue_name)
    for message in sent:
        sqs_queue.send_message(MessageBody=message)

    # Act

    received = []

    def create_client():
        return get_session().create_client("sqs", **local_sqs)

    sqs_broker = SqsBroker(client_factory=create_client)

    @sqs_broker.queue_consumer(queue_name)
    @flow.handler
    async def handle(m: SqsMessage):
        nonlocal received
        received += [m.body]

    host = Host(handle)
    async with host.aserve():
        await anyio.sleep(3)  # "App is working"
        host.shutdown()

    # Assert

    assert host.status["exception"] is None
    assert received == sent


async def test_batching(local_sqs):
    queue_name = "test_" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))

    # Arrange

    sent = ["hello", "world", "!"]
    sqs_queue = boto3.resource("sqs", **local_sqs).create_queue(QueueName=queue_name)
    for message in sent:
        sqs_queue.send_message(MessageBody=message)

    # Act

    received = []

    def create_client():
        return get_session().create_client("sqs", **local_sqs)

    sqs_broker = SqsBroker(client_factory=create_client)

    @sqs_broker.queue_consumer(queue_name)
    @flow_ops.batch(10, 1)
    @flow.handler
    async def handle(messages: Collection[SqsMessage]):
        nonlocal received
        received += [
            [m.body for m in messages]
        ]

    host = Host(handle)
    async with host.aserve():
        await anyio.sleep(3)  # "App is working"
        host.shutdown()

    # Assert

    assert host.status["exception"] is None
    assert received == [[m for m in sent]]
