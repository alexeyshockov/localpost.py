from __future__ import annotations

import dataclasses as dc
import logging
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager, AsyncExitStack, asynccontextmanager
from typing import Any, final

import confluent_kafka
from anyio import CapacityLimiter, create_task_group, from_thread, to_thread
from confluent_kafka import TIMESTAMP_NOT_AVAILABLE, Consumer

from localpost._utils import EventView
from localpost.flow import AnyHandlerManager, SyncHandlerManager, ensure_sync_handler_manager
from localpost.hosting import ExposedServiceBase, ServiceLifetimeManager

__all__ = [
    "KafkaMessage",
    "KafkaMessages",
    "KafkaConsumer",
    # "KafkaBroker",
    "kafka_config",
    "kafka_consumer",
]

logger = logging.getLogger(__name__)


@final
@dc.dataclass(frozen=True, slots=True, eq=False)
class KafkaMessage(AbstractContextManager[bytes, None]):
    payload: confluent_kafka.Message
    _client: Consumer
    _client_config: Mapping[str, Any]

    def __repr__(self):
        return f"{self.__class__.__name__}(topic={self.payload.topic()!r}"

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
        actually disabled.
        """
        if not self._client_config.get("enable.auto.offset.store", True):
            self.ack()

    def ack(self) -> None:
        """
        Store the offset of the message, so it won't be redelivered.

        Works only if 'enable.auto.offset.store' is set to False!
        """
        self._client.store_offsets(self.payload)  # Actual commit is done in the background


@final
@dc.dataclass(frozen=True, slots=True)
class KafkaMessages(Sequence[KafkaMessage], AbstractContextManager[Sequence[bytes], None]):
    """
    Non-empty batch of Kafka messages.
    """

    payload: Sequence[KafkaMessage]

    def __init__(self, payload: Sequence[KafkaMessage]):
        if isinstance(payload, KafkaMessages):
            payload = payload.payload
        assert payload
        object.__setattr__(self, "payload", payload)

    def __enter__(self) -> Sequence[bytes]:
        return [msg.value for msg in self.payload]

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


@final
class KafkaConsumer(ExposedServiceBase):
    def __init__(
        self,
        handler: SyncHandlerManager[KafkaMessage],
        topics: Iterable[str],
        /,
        *,
        client_config: Mapping[str, Any] | None = None,
        consumers: int = 1,
    ):
        super().__init__()
        if consumers < 1:
            raise ValueError("At least one consumer is required")

        self.client_config: dict[str, Any] = dict(client_config or {})
        self.topics = list(topics)
        self.handler = handler
        self.consumers = consumers
        self.poll_timeout = 0.5

    @asynccontextmanager
    async def _create_client(self):
        # TODO stats_cb, to provide detailed debug information
        # https://docs.confluent.io/platform/current/clients/confluent-kafka-python/html/index.html#kafka-client-configuration
        # https://github.com/confluentinc/librdkafka/blob/master/STATISTICS.md
        client = Consumer(
            self.client_config,
            logger=logger,  # noqa
        )
        await to_thread.run_sync(client.subscribe, self.topics)  # type: ignore
        try:
            yield client
        finally:
            await to_thread.run_sync(client.close)  # type: ignore

    def _run_consumer(
        self,
        client: Consumer,
        message_handler: Callable[[KafkaMessage], None],
        shutting_down: EventView,
    ) -> None:
        while not shutting_down:
            from_thread.check_cancelled()
            poll_res = client.poll(self.poll_timeout)  # Poll with a short timeout, so we can respect the cancellation
            if poll_res is None:
                continue  # Empty poll, check for cancellation and continue
            if error := poll_res.error():
                if error.retriable():
                    logger.warning("Kafka (non-fatal) error: [%s] %s", error.code(), error.str())
                    continue
                if error.fatal():
                    raise RuntimeError(error.str())
            message = KafkaMessage(poll_res, client, self.client_config)
            message_handler(message)

    async def __call__(self, service_lifetime: ServiceLifetimeManager):
        assert self.consumers > 0
        threads_limiter = CapacityLimiter(self.consumers)
        self._lifetime = service_lifetime

        def _consumer_thread(c):
            return to_thread.run_sync(
                self._run_consumer,
                c,
                message_handler,
                service_lifetime.shutting_down,
                # A custom limiter, to not reduce the global capacity permanently
                limiter=threads_limiter,
            )

        # Make sure to create a task group _after_ resolving the handler, so we exit it only after all the consumer
        # tasks are done
        async with AsyncExitStack() as clients, self.handler as message_handler, create_task_group() as tg:
            service_lifetime.set_started()
            await service_lifetime.host.started  # Start pulling messages only after the whole app is started
            for _ in range(self.consumers):
                client = await clients.enter_async_context(self._create_client())
                tg.start_soon(_consumer_thread, client)


def kafka_config_from_env() -> dict[str, Any]:
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

    return dict(_read_env_vars())


def kafka_config(**overrides) -> dict[str, Any]:
    conf_from_args = {k.replace("_", "."): v for k, v in overrides.items()}
    conf_from_env = kafka_config_from_env()
    return conf_from_env | conf_from_args


# @final
# class KafkaBroker:
#     """
#     Convenient way to create and register Kafka consumers.
#     """
#
#     def __init__(self, **config):
#         conf_from_args = {k.replace("_", "."): v for k, v in config.items()}
#         conf_from_env = kafka_conf_from_env()
#         self.client_config = conf_from_env | conf_from_args
#
#     def topic_consumer(
#         self, topics: str | Iterable[str], /, *, consumers: int = 1
#     # ) -> Callable[[HandlerManager[KafkaMessage] | SyncHandlerManager[KafkaMessage]], HostedService]:
#     ) -> Callable[[T], T]:
#         def _decorator(handler):
#             consumer = KafkaTopicConsumer(
#                 ensure_sync_handler(handler),
#                 [topics] if isinstance(topics, str) else topics,
#                 client_config=self.client_config,
#                 consumers=consumers,
#             )
#             # return HostedService(consumer, name=f"KafkaTopicConsumer({topics!r})")
#             return handler
#
#         return _decorator


# PyCharm (at least 2024.3) does not infer the changed type if it's a method, only when it's a function
def kafka_consumer(
    topics: str | Iterable[str], client_config: Mapping[str, Any] | None = None, /, *, consumers: int = 1
) -> Callable[[AnyHandlerManager[KafkaMessage]], KafkaConsumer]:
    def decorator(handler: AnyHandlerManager[KafkaMessage]):
        consumer = KafkaConsumer(
            ensure_sync_handler_manager(handler),
            [topics] if isinstance(topics, str) else topics,
            client_config=client_config,
            consumers=consumers,
        )
        return consumer

    return decorator
