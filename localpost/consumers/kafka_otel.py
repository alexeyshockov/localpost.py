from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractContextManager, contextmanager
from typing import TypeVar

from opentelemetry.metrics import MeterProvider, get_meter_provider
from opentelemetry.semconv._incubating.metrics.messaging_metrics import (  # noqa
    create_messaging_client_consumed_messages,
    create_messaging_client_operation_duration,
)
from opentelemetry.trace import SpanKind, TracerProvider, get_tracer_provider
from opentelemetry.util.types import AttributeValue

from localpost import __version__
from localpost._otel_utils import rec_duration
from localpost.consumers.kafka import KafkaMessage
from localpost.flow import FlowHandler, HandlerDecorator, handler_middleware

T = TypeVar("T", KafkaMessage, Sequence[KafkaMessage])
R = TypeVar("R", Awaitable[None], None)

__all__ = ["trace"]


def create_message_tracer(
    tp: TracerProvider | None,
    mp: MeterProvider | None,
) -> Callable[[KafkaMessage | Sequence[KafkaMessage]], AbstractContextManager[None]]:
    tracer = (tp or get_tracer_provider()).get_tracer(__name__, __version__)
    meter = (mp or get_meter_provider()).get_meter(__name__, __version__)

    # Based on Semantic Conventions 1.30.0, see
    # https://opentelemetry.io/docs/specs/semconv/messaging/messaging-spans/

    m_process_duration = create_messaging_client_operation_duration(meter)
    messages_consumed = create_messaging_client_consumed_messages(meter)

    @contextmanager
    def call_tracer(message: KafkaMessage | Sequence[KafkaMessage]):
        is_batch = isinstance(message, KafkaMessage)
        topic = message.payload.topic() if isinstance(message, KafkaMessage) else message[0].payload.topic()
        attrs: dict[str, AttributeValue] = {
            "messaging.operation.type": "process",
            "messaging.system": "kafka",
            "messaging.destination.name": topic,
        }
        if is_batch:
            attrs["messaging.batch.message_count"] = len(message)
        else:
            attrs["messaging.kafka.partition"] = (message.payload.partition(),)

        messages_consumed.add(len(message) if is_batch else 1, attrs)
        with tracer.start_as_current_span(f"process {topic}", kind=SpanKind.CONSUMER, attributes=attrs):
            with rec_duration(m_process_duration, attrs):
                yield

    return call_tracer


def trace(tp: TracerProvider | None = None, mp: MeterProvider | None = None, /) -> HandlerDecorator[T, T]:
    @handler_middleware
    async def middleware(next_h: FlowHandler):
        call_tracer = create_message_tracer(tp, mp)

        async def _handle_async(item):
            with call_tracer(item):
                await next_h.async_h(item)

        def _handle_sync(item):
            with call_tracer(item):
                next_h.sync_h(item)

        yield next_h.create(async_h=_handle_async, sync_h=_handle_sync)

    return middleware
