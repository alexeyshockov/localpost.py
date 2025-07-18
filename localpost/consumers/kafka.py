import dataclasses as dc
import logging
import os
from collections.abc import Callable, Collection, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, AbstractContextManager, AsyncExitStack, asynccontextmanager
from functools import partial
from typing import Any, Final, TypeAlias, cast, final, overload

import confluent_kafka
from anyio import CancelScope, CapacityLimiter, create_task_group, from_thread, to_thread
from confluent_kafka import TIMESTAMP_NOT_AVAILABLE

from localpost.flow import AnyHandlerManager, SyncHandler, ensure_sync_handler
from localpost.hosting import ExposedServiceBase, ServiceLifetimeManager

__all__ = [
    "KafkaMessage",
    "KafkaMessages",
    "ConsumerClient",
    "kafka_config_from_env",
    "kafka_consumer",
]

logger = logging.getLogger(__name__)

CHECK_INTERVAL: Final = 0.5  # seconds


@final
class ConsumerClient:
    @classmethod
    @asynccontextmanager
    async def create(cls, *topics: str, **config):
        rdkafka_config = {k.lower().replace("_", "."): v for k, v in config.items()}
        # https://docs.confluent.io/platform/current/clients/confluent-kafka-python/html/index.html#consumer
        transport = confluent_kafka.Consumer(rdkafka_config, logger=logger)  # type: ignore[call-arg]
        client = cls(transport, rdkafka_config, topics)
        try:
            await to_thread.run_sync(transport.subscribe, client.topics)  # type: ignore[call-arg]
            yield client
        finally:
            await to_thread.run_sync(transport.close)

    def __init__(self, transport: confluent_kafka.Consumer, config: Mapping[str, Any], topics: Collection[str]) -> None:
        if not topics:
            raise ValueError("At least one topic must be specified")

        self.config: Mapping[str, Any] = dict(config)
        self.topics: Sequence[str] = list(topics)
        self._client = transport

    def poll(self) -> confluent_kafka.Message | None:
        return self._client.poll(CHECK_INTERVAL)  # Interrupt periodically, so we can respect the cancellation

    def store_offset(self, message: confluent_kafka.Message) -> None:
        """
        Store the offset of the message, so it won't be redelivered (but only when `enable.auto.offset.store` is
        actually disabled).
        """
        if not self.config.get("enable.auto.offset.store", True):
            self._client.store_offsets(message)


ClientFactory: TypeAlias = Callable[[], AbstractAsyncContextManager[ConsumerClient]]


@final
@dc.dataclass(frozen=True, eq=False, slots=True)
class KafkaMessage(Sequence["KafkaMessage"], AbstractContextManager[bytes, None]):
    payload: confluent_kafka.Message
    _client: ConsumerClient

    def __repr__(self):
        return f"{self.__class__.__name__}(topic={self.payload.topic()!r})"

    def __len__(self):
        return 1

    def __getitem__(self, i):
        if i == 0:
            return self
        raise IndexError()

    def __enter__(self) -> bytes:
        return self.value

    def __exit__(self, exc_type, _, __) -> None:
        if exc_type is None:
            self.try_ack()

    @property
    def key(self) -> bytes | None:
        return self.payload.key()

    @property
    def timestamp(self) -> int | None:
        ts_type, ts = self.payload.timestamp()
        return None if ts_type == TIMESTAMP_NOT_AVAILABLE else ts

    @property
    def value(self) -> bytes:
        return self.payload.value()

    def try_ack(self) -> None:
        """
        Store the offset of the message, so it won't be redelivered (but only when `enable.auto.offset.store` is
        actually disabled).
        """
        if not self._client.config.get("enable.auto.offset.store", True):
            self.ack()

    def ack(self) -> None:
        """
        Store the offset of the message, so it won't be redelivered.

        Works only if 'enable.auto.offset.store' is set to False!
        """
        self._client.store_offset(self.payload)  # Actual commit is done in the background


@final
@dc.dataclass(frozen=True, slots=True)
class KafkaMessages(Sequence[KafkaMessage], AbstractContextManager[Sequence[bytes], None]):
    """
    Non-empty batch of Kafka messages.
    """

    source: Sequence[KafkaMessage]

    def __init__(self, source: Sequence[KafkaMessage]) -> None:
        if isinstance(source, KafkaMessages):
            source = source.source
        if len(source) == 0:
            raise ValueError(f"{self.__class__.__name__} must not be empty")
        object.__setattr__(self, "source", source)

    def __repr__(self):
        return f"{self.__class__.__name__}(len={len(self)})"

    def __len__(self):
        return len(self.payload)

    def __getitem__(self, i) -> KafkaMessage:
        return self.source[i]  # type: ignore[return-value]

    def __enter__(self) -> Sequence[bytes]:
        return [msg.value for msg in self.source]

    def __exit__(self, exc_type, _, __) -> None:
        if exc_type is None:
            self.try_ack()

    @property
    def payload(self) -> Sequence[confluent_kafka.Message]:
        return [msg.payload for msg in self.source]

    def try_ack(self) -> None:
        for message in self.payload:
            message.try_ack()

    def ack(self) -> None:
        for message in self.payload:
            message.ack()


async def _consume_sync(
    client: ConsumerClient, message_handler: SyncHandler[KafkaMessage], shutdown_scope: CancelScope
) -> None:
    def consume():
        while True:
            from_thread.check_cancelled()
            poll_res = client.poll()
            if poll_res is None:
                continue
            if error := poll_res.error():
                if error.retriable():
                    logger.warning("Kafka (non-fatal) error: [%s] %s", error.code(), error.str())
                    continue
                if error.fatal():
                    raise RuntimeError(error.str())
            message = KafkaMessage(poll_res, client)
            message_handler(message)

    # In Trio shutdown_scope.cancel_called can only be checked in async context
    with shutdown_scope:
        await to_thread.run_sync(consume, limiter=CapacityLimiter(1))


@final
class KafkaConsumerService(ExposedServiceBase):
    def __init__(
        self,
        cf: ClientFactory,
        handler_m: AnyHandlerManager[KafkaMessage],
        /,
        *,
        num_consumers: int,
    ):
        super().__init__()

        if num_consumers < 1:
            raise ValueError("Number of consumers must be at least 1")

        self.client_factory = cf
        self.handler_m = handler_m
        self.num_consumers = num_consumers

    async def __call__(self, service_lifetime: ServiceLifetimeManager) -> None:
        assert self.num_consumers > 0
        self._lifetime = service_lifetime  # Expose service lifetime events

        # Graceful shutdown scopes, one per consumer task (thread)
        consumer_scopes = [CancelScope() for _ in range(self.num_consumers)]

        async with AsyncExitStack() as client_stack, self.handler_m as handler:
            logger.debug("Creating Kafka clients (1 per consumer)...")
            # Although Confluent Kafka's Consumer is thread-safe, it's not intended to be used concurrently:
            #  - by default, a message received from poll() is automatically acknowledged
            #  - OTEL instrumentation stores the current span in an object field, so concurrent calls to poll() will
            #    mess up the traces
            clients = [await client_stack.enter_async_context(self.client_factory()) for _ in range(self.num_consumers)]
            message_handler = ensure_sync_handler(handler)
            service_lifetime.set_started()
            # Start pulling messages only after the whole app is started
            await service_lifetime.host.started
            async with create_task_group() as tg:
                for c, cs in zip(clients, consumer_scopes, strict=False):
                    tg.start_soon(_consume_sync, c, message_handler, cs)
                await service_lifetime.shutting_down
                for cs in consumer_scopes:
                    cs.cancel()


def kafka_client_factory(*topics: str, **config) -> ClientFactory:
    return partial(ConsumerClient.create, *topics, **config)


def kafka_config_from_env(**overrides) -> dict[str, Any]:
    """
    Construct a configuration dictionary from KAFKA_* environment variables.

    When translating Kafka's properties, use upper case and replace "." with "_":
     - bootstrap.servers -> KAFKA_BOOTSTRAP_SERVERS
     - max.in.flight.requests.per.connection -> KAFKA_MAX_IN_FLIGHT_REQUESTS_PER_CONNECTION
     - ...

    Properties reference: https://github.com/confluentinc/librdkafka/blob/master/CONFIGURATION.md
    See also: https://docs.confluent.io/platform/current/clients/confluent-kafka-python/html/index.html#pythonclient-configuration
    """

    def _read_env_vars():
        for var_name, var_val in os.environ.items():
            if var_name.startswith("KAFKA_"):
                yield var_name[6:].lower(), var_val

    conf_from_env = dict(_read_env_vars())
    return conf_from_env | overrides


@overload
def kafka_consumer(
    cf: ClientFactory, /, *, num_consumers: int = 1
) -> Callable[[AnyHandlerManager[KafkaMessage]], KafkaConsumerService]:
    """Decorator to create a Kafka consumer hosted service."""


@overload
def kafka_consumer(
    *topics: str, num_consumers: int = 1, **config: Any
) -> Callable[[AnyHandlerManager[KafkaMessage]], KafkaConsumerService]:
    """Decorator to create a Kafka consumer hosted service."""


# PyCharm (at least 2024.3) does not infer the changed type if it's a method, only when it's a function
def kafka_consumer(
    *topics_or_cf: Any, num_consumers: int = 1, **config: Any
) -> Callable[[AnyHandlerManager[KafkaMessage]], KafkaConsumerService]:
    if len(topics_or_cf) == 1 and callable(topics_or_cf[0]):
        cf = cast(ClientFactory, topics_or_cf[0])
    else:
        cf = kafka_client_factory(*topics_or_cf, **config)
    return lambda handler_m: KafkaConsumerService(cf, handler_m, num_consumers=num_consumers)
