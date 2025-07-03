from __future__ import annotations

import dataclasses as dc
import logging
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, AbstractContextManager
from typing import TYPE_CHECKING, Final, TypeAlias, TypedDict, cast, final

from aiobotocore.session import get_session
from anyio import CancelScope, create_task_group

from localpost import flow
from localpost._utils import ensure_async_callable, MemoryStream
from localpost.flow import AnyHandlerManager, AsyncHandlerManager, FlowHandlerManager, ensure_async_handler_manager, \
    AsyncHandler, ensure_async_handler
from localpost.flow._stream import create_stream_consumer, BatchReceiver
from localpost.hosting import ExposedServiceBase, ServiceLifetimeManager
from localpost.hosting.utils import ThreadSafeMemorySendStream

if TYPE_CHECKING:
    from types_aiobotocore_sqs import SQSClient
    from types_aiobotocore_sqs.literals import MessageSystemAttributeNameType
    from types_aiobotocore_sqs.type_defs import MessageTypeDef, ReceiveMessageRequestTypeDef

__all__ = [
    "SqsMessage",
    "SqsMessages",
    "sqs_queue_consumer",
    "lambda_handler",
]

logger = logging.getLogger(__name__)

_EMPTY_RECEIVE: Final[Sequence["MessageTypeDef"]] = ()


@final
@dc.dataclass(frozen=True, eq=False, slots=True)
class ConsumerClient:
    queue_name: str
    queue_url: str
    _client: object

    @classmethod
    def create(cls, queue_name: str, queue_url: str) -> Self:
        import boto3
        client = boto3.client("sqs")

    def receive(self, receive_req: "ReceiveMessageRequestTypeDef") -> Sequence["MessageTypeDef"]:
        receive_req = receive_req | {"QueueUrl": self.queue_url}
        # TODO Check HTTP status and retry on errors (exponential backoff)
        pull_resp = await self._client.receive_message(**receive_req)
        return pull_resp.get("Messages", _EMPTY_RECEIVE)

    def delete(self, messages: SqsMessage | Iterable[SqsMessage]) -> None:
        if isinstance(message := messages, SqsMessage):
            await self._client.delete_message(
                QueueUrl=self.queue_url,
                ReceiptHandle=message.receipt_handle,
            )
        else:
            await self._client.delete_message_batch(
                QueueUrl=self.queue_url,
                Entries=[  # type: ignore
                    {"Id": str(i), "ReceiptHandle": message.receipt_handle}
                    for i, message in enumerate(messages)
                ],
            )


@final
@dc.dataclass(frozen=True, eq=False, slots=True)
class AsyncConsumerClient:
    queue_name: str
    queue_url: str
    _client: "SQSClient"

    async def receive(self, receive_req: "ReceiveMessageRequestTypeDef") -> Sequence["MessageTypeDef"]:
        receive_req = receive_req | {"QueueUrl": self.queue_url}
        # TODO Check HTTP status and retry on errors (exponential backoff)
        pull_resp = await self._client.receive_message(**receive_req)
        return pull_resp.get("Messages", _EMPTY_RECEIVE)

    async def delete(self, messages: SqsMessage | Iterable[SqsMessage]) -> None:
        if isinstance(message := messages, SqsMessage):
            await self._client.delete_message(
                QueueUrl=self.queue_url,
                ReceiptHandle=message.receipt_handle,
            )
        else:
            await self._client.delete_message_batch(
                QueueUrl=self.queue_url,
                Entries=[  # type: ignore
                    {"Id": str(i), "ReceiptHandle": message.receipt_handle}
                    for i, message in enumerate(messages)
                ],
            )


AsyncClientFactory: TypeAlias = Callable[[], AbstractAsyncContextManager[AsyncConsumerClient]]


@final
@dc.dataclass(frozen=True, slots=True)
class SqsMessage(AbstractContextManager["SqsMessage", None]):
    payload: "MessageTypeDef"
    """
    Raw message data from the SQS queue.

    See https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_Message.html.
    """
    client: AsyncConsumerClient
    ack_queue: ThreadSafeMemorySendStream[SqsMessage]

    def __repr__(self):
        return f"{self.__class__.__name__}(queue_name={self.client.queue_name!r})"

    def __exit__(self, exc_type, exc_value, traceback) -> None:
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

    @property
    def attributes(self):
        return self.payload.get("MessageAttributes", {})


@final
@dc.dataclass(frozen=True, slots=True)
class SqsMessages(Sequence[SqsMessage], AbstractContextManager[Sequence[SqsMessage], None]):
    """ Non-empty batch of SQS messages. """

    source: Sequence[SqsMessage]

    def __init__(self, source: Sequence[SqsMessage]):
        if isinstance(source, SqsMessages):
            source = source.source
        if len(source) == 0:
            raise ValueError(f"{self.__class__.__name__} must not be empty")
        object.__setattr__(self, "source", source)

    def __repr__(self):
        return f"{self.__class__.__name__}(len={len(self)})"

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if exc_type is None:
            for message in self.source:
                message.ack()

    @property
    def payload(self) -> Sequence["MessageTypeDef"]:
        return [msg.payload for msg in self.source]

    def __getitem__(self, item):
        return self.source[item]

    def __len__(self):
        return len(self.source)


def _queue_name_from_url(url: str) -> str:
    from urllib.parse import urlparse

    parse_result = urlparse(url)
    return parse_result.path.split("/")[-1]


async def _queue_url_from_name(name: str, c: "SQSClient") -> str:
    resolve_resp = await c.get_queue_url(QueueName=name)
    return resolve_resp["QueueUrl"]


def client_factory(queue_name_or_url: str, /) -> AsyncClientFactory:
    """ Default SQS client factory. """
    @asynccontextmanager
    async def with_client():
        if "/" in queue_name_or_url:
            queue_url = queue_name_or_url
            queue_name = _queue_name_from_url(queue_url)
        else:
            queue_url = None
            queue_name = queue_name_or_url
        async with get_session().create_client("sqs") as transport:
            if queue_url is None:
                queue_url = await _queue_url_from_name(queue_name, transport)
            client = AsyncConsumerClient(queue_name, queue_url, transport)
            yield client

    return with_client  # type: ignore[return-value]


@final
class SqsConsumerService(ExposedServiceBase):
    def __init__(
        self,
        cf: AsyncClientFactory,
        handler_m: AsyncHandlerManager[SqsMessage],
        /,
        *,
        consumers: int,
    ):
        super().__init__()
        if consumers < 1:
            raise ValueError("Number of consumers must be at least 1")

        self.handler_m = handler_m
        self.client_factory = cf
        self.consumers = consumers
        self.receive_req_template: "ReceiveMessageRequestTypeDef" = {
            "QueueUrl": "",  # Will be filled in later
            "MessageAttributeNames": ["All"],
            "MaxNumberOfMessages": 10,
            "WaitTimeSeconds": 20,
        }

    # See also https://github.com/aio-libs/aiobotocore/blob/master/examples/sqs_queue_consumer.py
    async def _consume(
        self,
        client: AsyncConsumerClient,
        message_handler: AsyncHandler[SqsMessage],
        ack_queue: ThreadSafeMemorySendStream[SqsMessage],
        shutdown_scope: CancelScope,
    ):
        while not shutdown_scope.cancel_called:
            messages = _EMPTY_RECEIVE
            with shutdown_scope:
                messages = await client.receive(self.receive_req_template)
            if not messages:
                continue  # No messages received (empty queue or shutdown)
            for m in messages:
                await message_handler(SqsMessage(m, client, ack_queue))

    async def __call__(self, service_lifetime: ServiceLifetimeManager):
        self._lifetime = service_lifetime  # Expose service lifetime events

        ack_queue_writer, ack_queue_reader = MemoryStream.create(math.inf)
        batched_ack_queue_reader = BatchReceiver(ack_queue_reader, batch_size=10, batch_window=1.0)

        # Graceful shutdown scopes, one per consumer
        consumer_scopes = [CancelScope() for _ in range(self.consumers)]
        async with (
            self.client_factory() as client,
            create_stream_consumer(batched_ack_queue_reader, client.delete, concurrency=math.inf),
            ack_queue_writer,
            self.handler_m as handler,
            create_task_group() as tg):
            message_handler = ensure_async_handler(handler)
            robust_ack_queue = ThreadSafeMemorySendStream(ack_queue_writer, service_lifetime.host)
            service_lifetime.set_started()
            # Start pulling messages only after the whole app is started
            await service_lifetime.host.started
            for consumer_scope in consumer_scopes:
                tg.start_soon(self._consume, client, message_handler, robust_ack_queue, consumer_scope)
            await service_lifetime.shutting_down
            for consumer_scope in consumer_scopes:
                consumer_scope.cancel()


# PyCharm (at least 2024.3) does not infer the changed type if it's a method, only when it's a function
def sqs_queue_consumer(
    cf: AsyncClientFactory, /, *, consumers: int = 1
) -> Callable[[AnyHandlerManager[SqsMessage]], SqsConsumerService]:
    """ Decorator to create an SQS queue consumer hosted service. """
    return lambda handler: SqsConsumerService(
        cf, ensure_async_handler_manager(handler),
        consumers=consumers,
    )


class LambdaEventRecordMessageAttributeValue(TypedDict):
    dataType: str
    stringValue: str
    binaryValue: bytes
    stringListValues: Sequence[str]
    binaryListValues: Sequence[bytes]


class LambdaEventRecord(TypedDict):
    """
    {
        "messageId": "059f36b4-87a3-44ab-83d2-661975830a7d",
        "receiptHandle": "AQEBwJnKyrHigUMZj6rYigCgxlaS3SLy0a...",
        "body": "Test message.",
        "attributes": {
            "ApproximateReceiveCount": "1",
            "SentTimestamp": "1545082649183",
            "SenderId": "AIDAIENQZJOLO23YVJ4VO",
            "ApproximateFirstReceiveTimestamp": "1545082649185"
        },
        "messageAttributes": {
            "myAttribute": {
                "stringValue": "myValue",
                "stringListValues": [],
                "binaryListValues": [],
                "dataType": "String"
            }
        },
        "md5OfBody": "e4e68fb7bd0e697a0ae8f1bb342846b3",
        "eventSource": "aws:sqs",
        "eventSourceARN": "arn:aws:sqs:us-east-2:123456789012:my-queue",
        "awsRegion": "us-east-2"
    }
    """

    messageId: str
    receiptHandle: str
    body: str
    attributes: Mapping[MessageSystemAttributeNameType, str]
    messageAttributes: Mapping[str, LambdaEventRecordMessageAttributeValue]
    md5OfBody: str
    eventSource: str
    eventSourceARN: str
    awsRegion: str


class LambdaEvent(TypedDict):
    Records: Sequence[LambdaEventRecord]


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
) -> FlowHandlerManager[SqsMessage | Sequence[SqsMessage]]:
    lambda_inv_context = LambdaInvocationContext()
    async_lambda_h = ensure_async_callable(lambda_h)

    @flow.handler
    async def _handler(workload: SqsMessage | Sequence[SqsMessage]) -> None:
        lambda_event: LambdaEvent = {"Records": []}
        if isinstance(workload, SqsMessage):
            message = workload
            lambda_event["Records"] = [_message2lambda(message)]
            async with message:
                await async_lambda_h(lambda_event, lambda_inv_context)
        else:
            messages = SqsMessages(cast(Sequence[SqsMessage], workload))
            lambda_event["Records"] = [_message2lambda(m) for m in messages]
            async with messages:
                await async_lambda_h(lambda_event, lambda_inv_context)

    return _handler
