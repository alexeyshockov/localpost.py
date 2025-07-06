import dataclasses as dc
import logging
import os
from collections.abc import Callable, Iterable, Mapping, Sequence, Collection
from contextlib import AbstractContextManager, AbstractAsyncContextManager, asynccontextmanager, AsyncExitStack
from functools import partial
from typing import Any, final, Final

import confluent_kafka
from anyio import from_thread, to_thread, create_task_group, CapacityLimiter, CancelScope
from confluent_kafka import TIMESTAMP_NOT_AVAILABLE

from localpost._utils import EventView
from localpost.flow import AnyHandlerManager, ensure_sync_handler, SyncHandler
from localpost.hosting import ExposedServiceBase, ServiceLifetimeManager

__all__ = [
    "KafkaMessage",
    "KafkaMessages",
    "kafka_config_from_env",
    "kafka_consumer",
]

logger = logging.getLogger(__name__)

CHECK_INTERVAL: Final = 0.5  # seconds


@final
@dc.dataclass(frozen=True, eq=False, slots=True)
class ConsumerClient:
    config: Mapping[str, Any]
    topics: Sequence[str]
    _client: confluent_kafka.Consumer

    def subscribe(self) -> None:
        self._client.subscribe(self.topics)

    def poll(self) -> confluent_kafka.Message | None:
        return self._client.poll(CHECK_INTERVAL)  # Interrupt periodically, so we can respect the cancellation

    def store_offset(self, message: confluent_kafka.Message) -> None:
        """
        Store the offset of the message, so it won't be redelivered (but only when `enable.auto.offset.store` is
        actually disabled).
        """
        if not self.config.get("enable.auto.offset.store", True):
            self._client.store_offsets(message)


@final
@dc.dataclass(frozen=True, eq=False, slots=True)
class KafkaMessage(Sequence["KafkaMessage"], AbstractContextManager[bytes, None]):
    payload: confluent_kafka.Message
    _client: ConsumerClient

    def __repr__(self):
        return f"{self.__class__.__name__}(topic={self.payload.topic()!r}"

    def __len__(self):
        return 1

    def __getitem__(self, _):
        return self

    def __enter__(self) -> bytes:
        return self.value

    def __exit__(self, exc_type, exc_value, traceback) -> None:
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

    def __post_init__(self):
        if len(self.source) == 0:
            raise ValueError(f"{self.__class__.__name__} must not be empty")

    @property
    def payload(self) -> Sequence[confluent_kafka.Message]:
        return [msg.payload for msg in self.source]

    def __enter__(self) -> Sequence[bytes]:
        return [msg.value for msg in self.source]

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if exc_type is None:
            self.try_ack()

    def __getitem__(self, item):
        return self.payload[item]

    def __len__(self):
        return len(self.payload)

    def try_ack(self) -> None:
        for message in self.payload:
            message.try_ack()

    def ack(self) -> None:
        for message in self.payload:
            message.ack()


def _consume_sync(
    client: ConsumerClient,
    message_handler: SyncHandler[KafkaMessage],
    shutdown_scope: CancelScope
) -> None:
    while not shutdown_scope.cancel_called:
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


@final
class KafkaConsumerService(ExposedServiceBase):
    def __init__(
        self,
        cf: Callable[[], AbstractAsyncContextManager[ConsumerClient]],
        handler_m: AnyHandlerManager[KafkaMessage],
        /,
        *,
        consumers: int,
    ):
        super().__init__()

        if consumers < 1:
            raise ValueError("Number of consumers must be at least 1")

        self.handler_m = handler_m
        self.client_factory = cf
        self.num_consumers = consumers

    async def __call__(self, service_lifetime: ServiceLifetimeManager) -> None:
        assert self.num_consumers > 0
        self._lifetime = service_lifetime  # Expose service lifetime events

        threads_limiter = CapacityLimiter(self.num_consumers)
        run_thread = partial(to_thread.run_sync, limiter=threads_limiter)

        # Graceful shutdown scopes, one per consumer task (thread)
        consumer_scopes = [CancelScope() for _ in range(self.num_consumers)]
        # Although Confluent Kafka's Consumer is thread-safe, it's not intended to be used concurrently:
        #  - by default, a message received from poll() is automatically acknowledged
        #  - OTEL instrumentation stores the current span in an object field, so concurrent calls to poll() will mess
        #    up the traces
        clients = []

        async with AsyncExitStack() as client_stack, self.handler_m as handler:
            logger.debug("Creating Kafka clients (1 per consumer)...")
            for _ in consumer_scopes:
                clients.append(await client_stack.enter_async_context(self.client_factory()))
            logger.debug("Subscribing Kafka clients to topics...")
            async with create_task_group() as tg:
                for c in clients:
                    tg.start_soon(run_thread, c.subscribe)
            message_handler = ensure_sync_handler(handler)
            service_lifetime.set_started()
            # Start pulling messages only after the whole app is started
            await service_lifetime.host.started
            async with create_task_group() as tg:
                for c, cs in zip(clients, consumer_scopes):
                    tg.start_soon(run_thread,_consume_sync, c, message_handler, cs)
                await service_lifetime.shutting_down
                for cs in consumer_scopes:
                    cs.cancel()


@dc.dataclass(frozen=True, slots=True)
class ClientFactory:
    config: Mapping[str, Any]
    topics: Collection[str]

    @asynccontextmanager
    async def __call__(self):
        transport =  confluent_kafka.Consumer(
            self.config,
            logger=logger,  # noqa
        )
        client = ConsumerClient(dict(self.config), list(self.topics), transport)
        try:
            yield client
        finally:
            await to_thread.run_sync(transport.close)


def kafka_config_from_env(**overrides) -> dict[str, Any]:
    """
    Construct a configuration dictionary for KAFKA_* environment variables.

    When translating Kafka's properties, use upper case instead and replace the . with _ (KAFKA_BOOTSTRAP_SERVERS ->
    bootstrap.servers, etc.).

    Properties reference: https://github.com/confluentinc/librdkafka/blob/master/CONFIGURATION.md.
    """

    def _read_env_vars():
        for var_name, var_val in os.environ.items():
            if var_name.startswith("KAFKA_"):
                yield var_name[6:].lower().replace("_", "."), var_val

    conf_from_env = dict(_read_env_vars())
    conf_from_args = {k.replace("_", "."): v for k, v in overrides.items()}
    return conf_from_env | conf_from_args


# PyCharm (at least 2024.3) does not infer the changed type if it's a method, only when it's a function
def kafka_consumer(
    topics: str | Iterable[str], client_config: Mapping[str, Any] | None = None, /, *, consumers: int = 1
) -> Callable[[AnyHandlerManager[KafkaMessage]], KafkaConsumerService]:
    """ Decorator to create a Kafka consumer hosted service. """
    return lambda handler_m: KafkaConsumerService(
        ClientFactory(
            kafka_config_from_env(**(client_config or {})),
            [topics] if isinstance(topics, str) else list(topics)
        ),
        handler_m,
        consumers=consumers,
    )
