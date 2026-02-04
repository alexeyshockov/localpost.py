from __future__ import annotations

import dataclasses as dc
import logging
import math
from collections.abc import Callable, Iterable, Sequence
from contextlib import AbstractAsyncContextManager, AbstractContextManager, asynccontextmanager
from functools import partial
from typing import Final, TypeAlias, final, overload

from anyio import CancelScope, CapacityLimiter, create_task_group, from_thread, to_thread
from google.api_core import retry
from google.api_core.exceptions import AlreadyExists
from google.api_core.gapic_v1.method import DEFAULT
import google.pubsub_v1 as pubsub
from google.pubsub_v1 import SubscriberAsyncClient, SubscriberClient

from localpost._utils import MemoryStream
from localpost.flow import (
    AnyHandlerManager,
    AsyncHandler,
    SyncHandler,
    ensure_async_handler,
    ensure_sync_handler,
)
from localpost.flow._stream import BatchReceiver, create_stream_consumer
from localpost.hosting import ExposedServiceBase, ServiceLifetimeManager

__all__ = [
    "PubSubMessage",
    "PubSubMessages",
    "ConsumerClient",
    "client_factory",
    "pubsub_consumer",
]

from localpost.hosting.utils import ThreadSafeMemorySendStream, ThreadSafeSendStream

logger = logging.getLogger(__name__)

# Timeout for a sync pull request, to check the application state (and exit gracefully on shutdown)
CHECK_INTERVAL: Final = 3.0  # seconds

_EMPTY_RECEIVE: Final[Sequence[pubsub.ReceivedMessage]] = ()  # Empty response from pull()


@final
@dc.dataclass(frozen=True, eq=False, slots=True)
class PubSubMessage(Sequence["PubSubMessage"], AbstractContextManager[bytes, None]):
    payload: pubsub.ReceivedMessage
    client: ConsumerClient | AsyncConsumerClient
    ack_queue: ThreadSafeSendStream[PubSubMessage]

    def __repr__(self):
        return f"{self.__class__.__name__}(subscription_path={self.client.subscription_path!r})"

    def __len__(self):
        return 1

    def __getitem__(self, i):
        if i == 0:
            return self
        raise IndexError()

    def __enter__(self) -> bytes:
        return self.data

    def __exit__(self, exc_type, _, __) -> None:
        if exc_type is None:
            self.ack()

    def ack(self):
        self.ack_queue.send_nowait(self)

    @property
    def data(self) -> bytes:
        return self.payload.message.data


@final
@dc.dataclass(frozen=True, slots=True)
class PubSubMessages(Sequence[PubSubMessage], AbstractContextManager[Sequence[bytes], None]):
    """Non-empty batch of PubSub messages."""

    source: Sequence[PubSubMessage]

    def __init__(self, source: Sequence[PubSubMessage]) -> None:
        if isinstance(source, PubSubMessages):
            source = source.source
        if len(source) == 0:
            raise ValueError(f"{self.__class__.__name__} must not be empty")
        object.__setattr__(self, "source", source)

    def __repr__(self):
        subscription_path = self.source[0].client.subscription_path
        return f"{self.__class__.__name__}(len={len(self)}, subscription_path={subscription_path!r})"

    def __len__(self):
        return len(self.source)

    def __getitem__(self, i) -> PubSubMessage:
        return self.source[i]  # type: ignore[return-value]

    def __enter__(self) -> Sequence[bytes]:
        return self.data

    def __exit__(self, exc_type, _, __) -> None:
        if exc_type is None:
            for message in self.source:
                message.ack()

    @property
    def payload(self) -> Sequence[pubsub.ReceivedMessage]:
        return [msg.payload for msg in self.source]

    @property
    def data(self) -> Sequence[bytes]:
        return [msg.data for msg in self.source]


@final
class ConsumerClient:
    @classmethod
    @asynccontextmanager
    async def create(
        cls,
        subscription_path: str,  # projects/{project}/subscriptions/{subscription}
        topic_path: str,  # projects/{project}/topics/{topic}
        client: SubscriberClient | None = None,
        /,
        *,
        max_messages: int = 100,
        retry_policy: retry.Retry | object = DEFAULT,
    ):
        client = client or SubscriberClient()
        try:
            # In case the subscription needs to be customized, it can always be created in advance (so this call will
            # end up with ALREADY_EXISTS response), see also:
            # https://cloud.google.com/pubsub/docs/reference/error-codes
            try:
                logger.info("Creating subscription for pulling messages")
                await to_thread.run_sync(
                    client.create_subscription, {"name": subscription_path, "topic": topic_path})
            except AlreadyExists:
                logger.info("Subscription already exists, using it for pulling messages")
            yield cls(client, subscription_path, topic_path, max_messages, retry_policy)
        finally:
            await to_thread.run_sync(client.transport.close)

    def __init__(
        self,
        client: SubscriberClient,
        subscription_path: str,  # projects/{project}/subscriptions/{subscription}
        topic_path: str,  # projects/{project}/topics/{topic}
        max_messages: int,
        retry_policy: retry.Retry | object = DEFAULT,
    ):
        self.client = client
        self.subscription_path = subscription_path
        self.subscription_name = topic_path
        self.max_messages = max_messages
        self.retry_policy = retry_policy

    def acknowledge(self, target: Iterable[PubSubMessage]) -> None:
        request = {"subscription": self.subscription_path, "ack_ids": [msg.payload.ack_id for msg in target]}
        self.client.acknowledge(request)

    def pull(self):
        # The actual number of messages pulled may be smaller than max_messages
        response = self.client.pull(
            {"subscription": self.subscription_path, "max_messages": self.max_messages},
            retry=self.retry_policy or DEFAULT,
            timeout=CHECK_INTERVAL,
        )
        return response.received_messages


# https://cloud.google.com/pubsub/docs/samples/pubsub-subscriber-sync-pull#pubsub_subscriber_sync_pull-python
async def _consume_sync(
    client: ConsumerClient,
    message_handler: SyncHandler[PubSubMessage],
    ack_queue: ThreadSafeSendStream[PubSubMessage],
    shutdown_scope: CancelScope,
):
    def consume():
        while True:
            from_thread.check_cancelled()
            messages = client.pull()
            if not messages:
                continue  # No messages received (empty queue or shutdown)
            for m in messages:
                message_handler(PubSubMessage(m, client, ack_queue))

    # In Trio shutdown_scope.cancel_called can only be checked in async context
    with shutdown_scope:
        await to_thread.run_sync(consume, limiter=CapacityLimiter(1))


def client_factory(subscription_path: str) -> Callable[[], AbstractAsyncContextManager[ConsumerClient]]:
    """Default PubSub client factory."""
    return partial(ConsumerClient.create, subscription_path)


@final
class PubSubConsumerService(ExposedServiceBase):
    def __init__(
        self,
        cf: AnyClientFactory,
        handler_m: AnyHandlerManager[PubSubMessage],
        /,
        *,
        consumers: int,
    ):
        super().__init__()

        if consumers < 1:
            raise ValueError("Number of consumers must be at least 1")

        self.client_factory = cf
        self.handler_m = handler_m
        self.num_consumers = consumers

    async def __call__(self, service_lifetime: ServiceLifetimeManager) -> None:
        assert self.num_consumers > 0
        self._lifetime = service_lifetime  # Expose service lifetime events

        ack_queue_writer, ack_queue_reader = MemoryStream.create(math.inf)
        batched_ack_queue_reader = BatchReceiver[PubSubMessage, list[PubSubMessage]](
            ack_queue_reader, batch_size=100, batch_window=1.0
        )
        robust_ack_queue = ThreadSafeMemorySendStream(ack_queue_writer, service_lifetime.host)

        async def acknowledge_messages(messages: Iterable[PubSubMessage]) -> None:
            if isinstance(client, ConsumerClient):
                await to_thread.run_sync(client.acknowledge, messages)
            else:
                await client.acknowledge(messages)

        # Graceful shutdown scopes, one per consumer task (thread)
        consumer_scopes = [CancelScope() for _ in range(self.num_consumers)]

        async with (
            self.client_factory() as client,
            create_stream_consumer(batched_ack_queue_reader, acknowledge_messages, concurrency=math.inf),
            ack_queue_writer,
            self.handler_m as handler,
            create_task_group() as tg,
        ):
            service_lifetime.set_started()
            # Start pulling messages only after the whole app is started
            await service_lifetime.host.started
            if isinstance(client, ConsumerClient):
                sync_m_handler = ensure_sync_handler(handler)
                for cs in consumer_scopes:
                    tg.start_soon(_consume_sync, client, sync_m_handler, robust_ack_queue, cs)
            else:
                async_m_handler = ensure_async_handler(handler)
                for cs in consumer_scopes:
                    tg.start_soon(_consume_async, client, async_m_handler, robust_ack_queue, cs)
            await service_lifetime.shutting_down
            for cs in consumer_scopes:
                cs.cancel()


@overload
def pubsub_consumer(
    subscription_path: str, /, *, num_consumers: int = 1
) -> Callable[[AnyHandlerManager[PubSubMessage]], PubSubConsumerService]:
    """Decorator to create a PubSub consumer hosted service."""


@overload
def pubsub_consumer(
    cf: AnyClientFactory, /, *, num_consumers: int = 1
) -> Callable[[AnyHandlerManager[PubSubMessage]], PubSubConsumerService]:
    """Decorator to create a PubSub consumer hosted service."""


# PyCharm (at least 2024.3) does not infer the changed type if it's a method, only when it's a function
def pubsub_consumer(
    cf_or_sp: AnyClientFactory | str, /, *, num_consumers: int = 1
) -> Callable[[AnyHandlerManager[PubSubMessage]], PubSubConsumerService]:
    cf = client_factory(cf_or_sp) if isinstance(cf_or_sp, str) else cf_or_sp
    return lambda handler_m: PubSubConsumerService(cf, handler_m, consumers=num_consumers)
