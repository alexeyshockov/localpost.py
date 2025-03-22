from __future__ import annotations

import dataclasses as dc
import logging
import os
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, AbstractContextManager, AsyncExitStack, asynccontextmanager
from typing import Any, TypeAlias, final

import confluent_kafka
from anyio import CapacityLimiter, create_task_group, from_thread, to_thread
from confluent_kafka import TIMESTAMP_NOT_AVAILABLE, Consumer

from localpost._utils import EventView, is_async_callable
from localpost.hosting import ServiceLifetimeManager

__all__ = [
    "KafkaMessage",
    "KafkaHandler",
    "KafkaHandlerManager",
    "KafkaTopicConsumer",
    "KafkaBroker",
]

logger = logging.getLogger(__name__)


@final
@dc.dataclass(frozen=True, slots=True)
class KafkaMessage(AbstractContextManager[bytes, None]):
    payload: confluent_kafka.Message
    _client: Consumer = dc.field(repr=False)
    _client_config: Mapping[str, Any]

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
        if isinstance(payload, KafkaMessage):
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


KafkaHandler: TypeAlias = Callable[[KafkaMessage], Awaitable[object] | object]
KafkaHandlerManager: TypeAlias = AbstractAsyncContextManager[Callable[[KafkaMessage], Awaitable[object] | object]]


@final
class KafkaTopicConsumer:
    def __init__(
        self,
        handler: KafkaHandlerManager,
        topics: Iterable[str],
        /,
        *,
        client_config: dict[str, Any] | None = None,
        consumers: int = 1,
    ):
        if consumers < 1:
            raise ValueError("At least one consumer is required")

        self.client_config: dict[str, Any] = client_config or {}
        self.topics = list(topics)
        self.handler = handler
        self.consumers = consumers
        self.poll_timeout = 0.5

    @asynccontextmanager
    async def _create_client(self):
        # TODO stats_cb, to provide detailed debug information
        # https://docs.confluent.io/platform/current/clients/confluent-kafka-python/html/index.html#kafka-client-configuration
        # https://github.com/confluentinc/librdkafka/blob/master/STATISTICS.md
        client = Consumer(self.client_config, logger=logger)
        await to_thread.run_sync(client.subscribe, self.topics)  # type: ignore
        try:
            yield client
        finally:
            await to_thread.run_sync(client.close)  # type: ignore

    def _run_consumer(
        self,
        client: Consumer,
        message_handler: KafkaHandler,
        shutting_down: EventView,
    ) -> None:
        is_async_handler = is_async_callable(message_handler)
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
            if is_async_handler:
                from_thread.run(message_handler, message)
            else:
                message_handler(message)

    async def __call__(self, service_lifetime: ServiceLifetimeManager):
        assert self.consumers > 0
        threads_limiter = CapacityLimiter(self.consumers)

        def _run_consumer_thread(c, ss) -> Awaitable[None]:
            # Make sure to use a custom limiter for these long-running threads, to not reduce the global capacity
            # permanently
            return to_thread.run_sync(self._run_consumer, c, message_handler, ss, limiter=threads_limiter)

        # Make sure to create a task group _after_ resolving the handler, so we exit it only after all the consumer
        # tasks are done
        async with AsyncExitStack() as clients, self.handler as message_handler, create_task_group() as tg:
            for _ in range(self.consumers):
                client = await clients.enter_async_context(self._create_client())
                tg.start_soon(_run_consumer_thread, client, service_lifetime.shutting_down)

            service_lifetime.set_started()


def kafka_conf_from_env() -> dict[str, Any]:
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


@final
class KafkaBroker:
    """
    Convenient way to create and register Kafka consumers.
    """

    def __init__(self, **config):
        conf_from_args = {k.replace("_", "."): v for k, v in config.items()}
        conf_from_env = kafka_conf_from_env()
        self.client_config = conf_from_env | conf_from_args

    def topic_consumer(
        self, topics: str | Iterable[str], /, *, consumers: int = 1
    ) -> Callable[[KafkaHandlerManager], KafkaTopicConsumer]:
        def _decorator(handler: KafkaHandlerManager) -> KafkaTopicConsumer:
            consumer = KafkaTopicConsumer(
                handler,
                [topics] if isinstance(topics, str) else topics,
                client_config=self.client_config,
                consumers=consumers,
            )
            return consumer

        return _decorator
