from __future__ import annotations

import dataclasses as dc
import logging
import math
from collections.abc import Callable, Collection, Iterable, Sequence
from contextlib import AbstractAsyncContextManager, AbstractContextManager, asynccontextmanager
from functools import partial
from typing import Final, cast, final, overload
from urllib.parse import urlparse

from anyio import CancelScope, CapacityLimiter, create_task_group, from_thread, to_thread

from localpost import flow
from localpost._utils import MemoryStream, is_async_callable
from ._sqs_types import (
    BotoSqsClient,
    LambdaEvent,
    LambdaEventRecord,
    LambdaEventRecordMessageAttributeValue,
    MessageTypeDef,
    ReceiveMessageRequestTypeDef,
)

__all__ = [
    "SqsMessage",
    "SqsMessages",
    "ConsumerClient",
    "lambda_handler",
    "sqs_queue_consumer",
]

logger = logging.getLogger(__name__)

# Timeout for a sync pull request, to check the application state (and exit gracefully on shutdown)
CHECK_INTERVAL: Final = 3.0  # seconds

_EMPTY_RECEIVE: Final[Sequence[MessageTypeDef]] = ()


@final
class ConsumerClient:
    @classmethod
    @asynccontextmanager
    async def create(
        cls,
        queue_name_or_url: str,
        client: BotoSqsClient | None = None,
        /,
        *,
        req_template: ReceiveMessageRequestTypeDef | None = None,
    ):
        def _queue_url_from_name(n):
            return transport.get_queue_url(QueueName=n)["QueueUrl"]

        def get_client() -> BotoSqsClient:
            if client is None:
                try:
                    import boto3

                    return boto3.client("sqs")
                except ImportError:
                    from botocore.session import get_session

                    return get_session().create_client("sqs")  # type: ignore[return-value]
            return client

        transport = get_client()
        if "/" in queue_name_or_url:
            queue_url = queue_name_or_url
            queue_name = _queue_name_from_url(queue_url)
        else:
            queue_name = queue_name_or_url
            queue_url = await to_thread.run_sync(_queue_url_from_name, queue_name)
        yield cls(
            transport,
            queue_name,
            queue_url,
            req_template
            or {
                "QueueUrl": queue_name,
                "MessageAttributeNames": ["All"],
                "MaxNumberOfMessages": 10,
                "WaitTimeSeconds": int(CHECK_INTERVAL),  # Long polling
            },
        )

    def __init__(
        self,
        client: BotoSqsClient,
        queue_name: str,
        queue_url: str,
        receive_req: ReceiveMessageRequestTypeDef,
        /,
    ) -> None:
        self._client = client
        self.queue_name = queue_name
        self.queue_url = queue_url
        self.receive_req: ReceiveMessageRequestTypeDef = receive_req | {"QueueUrl": queue_url}

    def receive(self) -> Sequence[MessageTypeDef]:
        # TODO Check HTTP status and retry on errors (exponential backoff)
        pull_resp = self._client.receive_message(**self.receive_req)
        return pull_resp.get("Messages", _EMPTY_RECEIVE)

    def delete(self, workload: Iterable[SqsMessage], /) -> None:
        if isinstance(workload, SqsMessage):
            self._client.delete_message(
                QueueUrl=self.queue_url,
                ReceiptHandle=workload.receipt_handle,
            )
        else:
            self._client.delete_message_batch(
                QueueUrl=self.queue_url,
                Entries=[  # type: ignore
                    {"Id": str(i), "ReceiptHandle": message.receipt_handle} for i, message in enumerate(workload)
                ],
            )


@final
@dc.dataclass(frozen=True, slots=True)
class SqsMessage(Sequence["SqsMessage"], AbstractContextManager[str, None]):
    payload: MessageTypeDef
    """
    Raw message data from the SQS queue.

    See https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_Message.html.
    """

    client: ConsumerClient | AsyncConsumerClient
    ack_queue: ThreadSafeSendStream[SqsMessage]

    def __repr__(self):
        return f"{self.__class__.__name__}(queue_name={self.client.queue_name!r})"

    def __len__(self):
        return 1

    def __getitem__(self, i):
        if i == 0:
            return self
        raise IndexError()

    def __enter__(self) -> str:
        return self.body

    def __exit__(self, exc_type, _, __) -> None:
        if exc_type is None:
            self.ack()

    def ack(self):
        self.ack_queue.send_nowait(self)

    @property
    def receipt_handle(self):
        assert "ReceiptHandle" in self.payload
        return self.payload["ReceiptHandle"]

    @property
    def body(self) -> str:
        assert "Body" in self.payload
        return self.payload["Body"]

    # https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-message-metadata.html#message-attribute-data-types
    @property
    def attributes(self) -> dict[str, str | bool | int | float | bytes]:
        def extract_attrs():
            for attr_name, msg_attr in self.payload.get("MessageAttributes", {}).items():
                attr_type = msg_attr.get("DataType", "").lower()
                str_val = msg_attr.get("StringValue")
                if attr_type.startswith("string.bool"):
                    assert str_val
                    yield attr_name, str_val.lower() in ("true", "1", "yes")
                elif attr_type.startswith("string"):
                    assert str_val
                    yield attr_name, str_val
                elif attr_type.startswith("number"):
                    assert str_val
                    try:
                        yield attr_name, int(str_val)
                    except ValueError:
                        yield attr_name, float(str_val)
                elif attr_type.startswith("binary"):
                    yield attr_name, msg_attr["BinaryValue"]  # type: ignore

        return dict(extract_attrs())


@final
@dc.dataclass(frozen=True, slots=True)
class SqsMessages(Sequence[SqsMessage], AbstractContextManager[Sequence[str], None]):
    """Non-empty batch of SQS messages."""

    source: Sequence[SqsMessage]

    def __init__(self, source: Sequence[SqsMessage]):
        if isinstance(source, SqsMessages):
            source = source.source
        if len(source) == 0:
            raise ValueError(f"{self.__class__.__name__} must not be empty")
        object.__setattr__(self, "source", source)

    def __repr__(self):
        queue_name = self.source[0].client.queue_name
        return f"{self.__class__.__name__}(len={len(self)}, queue_name={queue_name!r})"

    def __len__(self):
        return len(self.source)

    def __getitem__(self, i) -> SqsMessage:
        return self.source[i]  # type: ignore[return-value]

    def __enter__(self) -> Sequence[str]:
        return [msg.body for msg in self.source]

    def __exit__(self, exc_type, _, __) -> None:
        if exc_type is None:
            for message in self.source:
                message.ack()

    @property
    def payload(self) -> Sequence[MessageTypeDef]:
        return [msg.payload for msg in self.source]


def _queue_name_from_url(url: str) -> str:
    parse_result = urlparse(url)
    return parse_result.path.split("/")[-1]


def client_factory(queue_name_or_url: str, /) -> Callable[[], AbstractAsyncContextManager[ConsumerClient]]:
    """Default SQS client factory."""
    return partial(ConsumerClient.create, queue_name_or_url)


def async_client_factory(queue_name_or_url: str, /) -> Callable[[], AbstractAsyncContextManager[AsyncConsumerClient]]:
    """Default SQS async (aioboto3) client factory."""
    return partial(AsyncConsumerClient.create, queue_name_or_url)


async def _consume_sync(
    client: ConsumerClient,
    message_handler: SyncHandler[SqsMessage],
    ack_queue: ThreadSafeSendStream[SqsMessage],
    shutdown_scope: CancelScope,
):
    def consume():
        while True:
            from_thread.check_cancelled()
            messages = client.receive()
            if not messages:
                continue  # No messages received (empty queue or shutdown)
            for m in messages:
                message_handler(SqsMessage(m, client, ack_queue))

    # In Trio shutdown_scope.cancel_called can only be checked in async context
    with shutdown_scope:
        await to_thread.run_sync(consume, limiter=CapacityLimiter(1))


@final
class SqsConsumerService(ExposedServiceBase):
    def __init__(
        self,
        cf: AnyClientFactory,
        handler_m: AnyHandlerManager[SqsMessage],
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

    async def __call__(self, service_lifetime: ServiceLifetimeManager):
        assert self.num_consumers > 0
        self._lifetime = service_lifetime  # Expose service lifetime events

        ack_queue_writer, ack_queue_reader = MemoryStream.create(math.inf)
        batched_ack_queue_reader = BatchReceiver[SqsMessage, list[SqsMessage]](
            ack_queue_reader, batch_size=10, batch_window=1.0
        )
        robust_ack_queue = ThreadSafeMemorySendStream(ack_queue_writer, service_lifetime.host)

        async def acknowledge_messages(messages: Collection[SqsMessage]) -> None:
            if isinstance(client, ConsumerClient):
                await to_thread.run_sync(client.delete, messages)
            else:
                await client.delete(messages)

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
def sqs_queue_consumer(
    queue_name_or_url: str, /, *, num_consumers: int = 1
) -> Callable[[AnyHandlerManager[SqsMessage]], SqsConsumerService]:
    """Decorator to create an SQS queue consumer (boto3) hosted service."""


@overload
def sqs_queue_consumer(
    cf: AnyClientFactory, /, *, num_consumers: int = 1
) -> Callable[[AnyHandlerManager[SqsMessage]], SqsConsumerService]:
    """Decorator to create an SQS queue consumer hosted service."""


# PyCharm (at least 2024.3) does not infer the changed type if it's a method, only when it's a function
def sqs_queue_consumer(
    cf_or_q: AnyClientFactory | str, /, *, num_consumers: int = 1
) -> Callable[[AnyHandlerManager[SqsMessage]], SqsConsumerService]:
    cf = client_factory(cf_or_q) if isinstance(cf_or_q, str) else cf_or_q
    return lambda handler_m: SqsConsumerService(cf, handler_m, num_consumers=num_consumers)


def _message2lambda(m: SqsMessage, /) -> LambdaEventRecord:
    # See https://docs.aws.amazon.com/lambda/latest/dg/with-sqs.html &
    # https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_ReceiveMessage.html#API_ReceiveMessage_ResponseSyntax
    return {
        "messageId": m.payload["MessageId"],  # type: ignore
        "receiptHandle": m.payload["ReceiptHandle"],  # type: ignore
        "body": m.payload["Body"],  # type: ignore
        "attributes": m.payload.get("Attributes", {}),
        "messageAttributes": {
            ma_name: cast(
                LambdaEventRecordMessageAttributeValue,
                {ma_k[0].lower() + ma_k[1:]: ma_v for ma_k, ma_v in ma_values.items()},
            )
            for ma_name, ma_values in m.payload.get("MessageAttributes", {}).items()
        },
        "md5OfBody": m.payload["MD5OfBody"],  # type: ignore
        "eventSource": "aws:sqs",
        "eventSourceARN": "TODO",
        "awsRegion": "TODO",
    }


@final
class LambdaInvocationContext:
    def __init__(self) -> None:
        # See https://docs.aws.amazon.com/lambda/latest/dg/python-context.html
        self.function_name = "N/A"
        self.function_version = "N/A"
        self.invoked_function_arn = "N/A"
        self.memory_limit_in_mb = 1024
        self.aws_request_id = "N/A"  # Use OTEL trace ID if available?..
        self.log_group_name = "N/A"
        self.log_stream_name = "N/A"
        self.identity = None
        self.client_context = None


def lambda_handler(
    lambda_h: Callable[[LambdaEvent, LambdaInvocationContext], object], /
) -> FlowHandlerManager[Sequence[SqsMessage]]:
    assert not is_async_callable(lambda_h)
    lambda_inv_context = LambdaInvocationContext()

    @flow.handler
    def _handler(workload: Sequence[SqsMessage]) -> None:
        lambda_event = cast(LambdaEvent, {"Records": []})
        messages = SqsMessages(workload)
        lambda_event["Records"] = [_message2lambda(m) for m in messages]
        with messages:
            lambda_h(lambda_event, lambda_inv_context)

    return _handler
