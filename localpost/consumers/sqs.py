from __future__ import annotations

import dataclasses as dc
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from typing import TypeAlias, cast, final

from aiobotocore.session import get_session
from anyio import CancelScope, create_task_group, to_thread
from types_aiobotocore_sqs import SQSClient
from types_aiobotocore_sqs.type_defs import MessageTypeDef, ReceiveMessageRequestRequestTypeDef
from typing_extensions import TypedDict

from localpost import flow
from localpost.flow import Handler, HandlerManager
from localpost.hosting import ServiceLifetimeManager

__all__ = [
    "delete_messages",
    "SqsMessage",
    "SqsQueueConsumer",
    "SqsBroker",
    "lambda_handler",
]

ClientFactory: TypeAlias = Callable[[], AbstractAsyncContextManager[SQSClient]]

logger = logging.getLogger(__name__)


async def _delete_message_chunk(messages: Sequence[SqsMessage]):
    client = messages[0]._client  # noqa
    queue_url = messages[0].queue_url
    await client.delete_message_batch(
        QueueUrl=queue_url,
        Entries=[
            {
                "Id": str(i),
                "ReceiptHandle": message.receipt_handle,
            }
            for i, message in enumerate(messages)
        ],
    )


def _split_messages(messages: Sequence[SqsMessage]) -> Iterable[Sequence[SqsMessage]]:
    partitions: dict[str, list[SqsMessage]] = {}
    for message in messages:
        partitions.setdefault(message.queue_url, []).append(message)
    for queue_messages in partitions.values():
        for i in range(0, len(queue_messages), 10):
            yield queue_messages[i : i + 10]


async def delete_messages(messages: Sequence[SqsMessage]):
    """
    Delete multiple messages from the queue.

    AWS supports batches up to 10 messages. If the input list is longer, it will be split into chunks, and all the
    chunks will be processed simultaneously.
    """
    async with create_task_group() as tg:
        for chunk in _split_messages(messages):
            tg.start_soon(_delete_message_chunk, chunk)


# See also https://github.com/aio-libs/aiobotocore/blob/master/examples/sqs_queue_consumer.py


@final
@dc.dataclass(frozen=True, slots=True)
class SqsMessage(AbstractAsyncContextManager[str, None]):
    payload: MessageTypeDef
    """
    Raw message data from the SQS queue.

    See https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_Message.html.
    """

    _consumer: SqsQueueConsumer
    _client: SQSClient

    def __repr__(self):
        return f"<SqsMessage(queue_name={self._consumer.queue_name})>"

    async def __aenter__(self) -> str:
        return self.body

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type is None:
            await self.ack()

    @property
    def queue_name(self) -> str:
        return self._consumer.queue_name

    @property
    def queue_url(self) -> str:
        return self._consumer.queue_url

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

    async def ack(self) -> None:
        """
        Delete the message from the queue (acknowledge), otherwise it will reappear after the visibility timeout.
        """
        await self._client.delete_message(
            QueueUrl=self._consumer.queue_url,
            ReceiptHandle=self.receipt_handle,
        )


@final
@dc.dataclass(frozen=True, slots=True)
class SqsMessages(Sequence[SqsMessage], AbstractAsyncContextManager[Sequence[str], None]):
    """
    Non-empty batch of SQS messages.
    """

    payload: Sequence[SqsMessage]

    def __init__(self, payload: Sequence[SqsMessage]):
        if isinstance(payload, SqsMessages):
            payload = payload.payload
        assert payload
        object.__setattr__(self, "payload", payload)

    async def __aenter__(self) -> Sequence[str]:
        return [m.body for m in self.payload]

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type is None:
            await self.ack()

    def __getitem__(self, item):
        return self.payload[item]

    def __len__(self):
        return len(self.payload)

    @property
    def queue_name(self) -> str:
        return self.payload[0].queue_name

    async def ack(self) -> None:
        """
        Delete the messages from the queue (acknowledge), otherwise they will reappear after the visibility timeout.
        """
        await delete_messages(self.payload)


def _queue_name_from_url(url: str) -> str:
    from urllib.parse import urlparse

    parse_result = urlparse(url)
    return parse_result.path.split("/")[-1]


def create_client() -> AbstractAsyncContextManager[SQSClient]:
    """
    Default SQS client factory.
    """
    return get_session().create_client("sqs")


@final
class SqsQueueConsumer:
    def __init__(
        self,
        handler: HandlerManager[SqsMessage],
        queue_name: str,
        *,
        queue_url: str | None = None,
        client_factory: ClientFactory | None = None,
        consumers: int = 1,
    ):
        if consumers < 1:
            raise ValueError("Number of consumers must be at least 1")

        self.queue_name = queue_name
        self.queue_url = queue_url
        self.handler = handler
        self.client_factory: ClientFactory = client_factory or create_client
        self.consumers = consumers
        self.receive_req_template: ReceiveMessageRequestRequestTypeDef = {
            "QueueUrl": "",  # Will be filled in later
            "MessageAttributeNames": ["All"],
            "MaxNumberOfMessages": 10,
            "WaitTimeSeconds": 20,
        }

    async def _run_consumer(
        self,
        client: SQSClient,
        message_handler: Handler[SqsMessage],
        shutdown_scope: CancelScope,
    ):
        queue_url = cast(str, self.queue_url)
        receive_req = cast(ReceiveMessageRequestRequestTypeDef, self.receive_req_template | {"QueueUrl": queue_url})
        no_messages: Sequence[MessageTypeDef] = []

        # TODO Check HTTP status and retry on errors (exponential backoff)
        async def pull_messages() -> Sequence[MessageTypeDef]:
            pull_resp = await client.receive_message(**receive_req)
            return pull_resp.get("Messages", no_messages)

        messages = no_messages
        while not shutdown_scope.cancel_called:
            with shutdown_scope:
                messages = await pull_messages()
            if shutdown_scope.cancel_called:
                break
            if not messages:
                logger.debug("No messages in the queue (empty receive)")
                continue
            for m in messages:
                await message_handler(SqsMessage(m, self, client))

    async def __call__(self, service_lifetime: ServiceLifetimeManager):
        async def _resolve_url_from_name():
            async with self.client_factory() as c:
                resolve_resp = await c.get_queue_url(QueueName=self.queue_name)
                return resolve_resp["QueueUrl"]

        self.queue_url = self.queue_url or await _resolve_url_from_name()

        consumer_scopes = [CancelScope() for _ in range(self.consumers)]
        # Make sure to create a task group _after_ resolving the handler, so we exit it only after all the consumer
        # tasks are done
        async with AsyncExitStack() as clients, self.handler as message_handler, create_task_group() as tg:
            for shutdown_scope in consumer_scopes:
                client = await clients.enter_async_context(self.client_factory())
                tg.start_soon(self._run_consumer, client, message_handler, shutdown_scope)

            service_lifetime.set_started()
            await service_lifetime.shutting_down.wait()
            for scope in consumer_scopes:
                scope.cancel()


@final
class SqsBroker:
    def __init__(self, *, client_factory: ClientFactory | None = None):
        self.client_factory = client_factory or create_client

    def queue_consumer(self, queue_name_or_url: str, /, *, consumers: int = 1):
        if "/" in queue_name_or_url:
            queue_url = queue_name_or_url
            queue_name = _queue_name_from_url(queue_url)
        else:
            queue_url = None
            queue_name = queue_name_or_url

        def _decorator(handler: HandlerManager[SqsMessage]) -> SqsQueueConsumer:
            consumer = SqsQueueConsumer(
                handler,
                queue_name=queue_name,
                queue_url=queue_url,
                client_factory=self.client_factory,
                consumers=consumers,
            )
            return consumer

        return _decorator


class LambdaEventRecord(TypedDict):
    messageId: str
    receiptHandle: str
    body: str
    attributes: Mapping[str, object]
    messageAttributes: Mapping[str, object]
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
        "messageId": m.payload["MessageId"],
        "receiptHandle": m.payload["ReceiptHandle"],
        "body": m.payload["Body"],
        "attributes": m.payload.get("Attributes", {}),
        "messageAttributes": {
            ma_name: {ma_k[0].lower() + ma_k[1:]: ma_v for ma_k, ma_v in ma_values.items()}
            for ma_name, ma_values in m.payload.get("MessageAttributes", {}).items()
        },
        "md5OfBody": m.payload["MD5OfBody"],
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
) -> HandlerManager[SqsMessage | Sequence[SqsMessage]]:
    lambda_inv_context = LambdaInvocationContext()

    @flow.handler
    async def _handler(workload: SqsMessage | Sequence[SqsMessage]) -> None:
        if isinstance(input, SqsMessage):
            message = workload
            lambda_event = {"Records": [_message2lambda(message)]}
            async with message:
                await to_thread.run_sync(lambda_h, lambda_event, lambda_inv_context)
        else:
            messages = SqsMessages(workload)
            lambda_event = {"Records": [_message2lambda(m) for m in messages]}
            async with messages:
                await to_thread.run_sync(lambda_h, lambda_event, lambda_inv_context)

    return _handler
