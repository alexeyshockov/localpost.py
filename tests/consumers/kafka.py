import logging
import random
import string

import anyio
import pytest
from confluent_kafka import Producer

from localpost import flow
from localpost.consumers.kafka import KafkaMessage, KafkaMessages, kafka_consumer
from localpost.hosting import Host

from .RedpandaContainer import RedpandaContainer

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

logger = logging.getLogger(__name__)


@pytest.fixture(scope="module")
def local_kafka():
    with RedpandaContainer("redpandadata/redpanda:v24.3.3") as kafka_broker:
        conn_config = {
            "bootstrap.servers": kafka_broker.get_bootstrap_server(),
        }
        yield conn_config


async def test_normal_case(local_kafka):
    topic_name = "test_" + "".join(random.choices(string.ascii_uppercase + string.digits, k=10))

    # Arrange

    sent = ["London: cloudy", "Paris: rainy"]
    p = Producer(local_kafka, logger=logger)
    for message in sent:  # Redpanda creates a topic automatically if it doesn't exist
        p.produce(topic_name, message)
    p.flush()

    # Act

    received = []
    client_config = local_kafka | {
        "group.id": "integration_tests",
        "auto.offset.reset": "earliest",
    }

    @kafka_consumer(topic_name, client_config)
    @flow.sync_handler
    def handle(m: KafkaMessage) -> None:
        received.append(m.value.decode())

    host = Host(handle)
    async with host.aserve():
        await anyio.sleep(3)  # "App is working"
        host.shutdown()

    # Assert

    assert host.status["exception"] is None
    assert received == sent


async def test_batching(local_kafka):
    topic_name = "test_" + "".join(random.choices(string.ascii_uppercase + string.digits, k=10))

    # Arrange

    sent = ["London: cloudy", "Paris: rainy"]
    p = Producer(local_kafka, logger=logger)
    for message in sent:
        p.produce(topic_name, message)
    p.flush()

    # Act

    received = []
    client_config = local_kafka | {
        "group.id": "integration_tests",
        "auto.offset.reset": "earliest",
    }

    @kafka_consumer(topic_name, client_config)
    @flow.batch(10, 1, KafkaMessages)
    @flow.handler
    async def handle(messages: KafkaMessages):
        nonlocal received
        received += [[m.value.decode() for m in messages]]

    host = Host(handle)
    async with host.aserve():
        await anyio.sleep(3)  # "App is working"
        host.shutdown()

    # Assert

    assert host.status["exception"] is None
    assert received == [sent]
