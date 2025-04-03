from __future__ import annotations

import time
from collections.abc import Awaitable, Sequence
from contextlib import contextmanager
from typing import TypeVar

from opentelemetry.metrics import MeterProvider, get_meter_provider
from opentelemetry.semconv._incubating.metrics.messaging_metrics import (  # noqa
    create_messaging_client_consumed_messages,
    create_messaging_client_operation_duration,
)
from opentelemetry.trace import SpanKind, TracerProvider, get_tracer_provider
from opentelemetry.util.types import AttributeValue

from localpost import __version__
from localpost.consumers.sqs import SqsMessage
from localpost.flow._flow import HandlerDecorator, handler_decorator_from_wrapper

T = TypeVar("T", SqsMessage, Sequence[SqsMessage])

__all__ = ["trace"]


def trace(
    tp: TracerProvider | None = None, mp: MeterProvider | None = None, /
) -> HandlerDecorator[T, Awaitable[None], T]:
    tracer = (tp or get_tracer_provider()).get_tracer(__name__, __version__)
    meter = (mp or get_meter_provider()).get_meter(__name__, __version__)

    # Based on Semantic Conventions 1.30.0, see
    # https://opentelemetry.io/docs/specs/semconv/messaging/messaging-spans/

    m_process_duration = create_messaging_client_operation_duration(meter)
    messages_consumed = create_messaging_client_consumed_messages(meter)

    @contextmanager
    def call_tracer(message: T):
        queue_name = message.queue_name if isinstance(message, SqsMessage) else message[0].queue_name
        attrs: dict[str, AttributeValue] = {
            "messaging.operation.type": "process",
            "messaging.system": "aws_sqs",
            "messaging.destination.name": queue_name,
        }
        if not isinstance(message, SqsMessage):
            attrs["messaging.batch.message_count"] = len(message)

        messages_consumed.add(1 if isinstance(message, SqsMessage) else len(message), attrs)
        with tracer.start_as_current_span(f"process {queue_name}", kind=SpanKind.CONSUMER, attributes=attrs):
            start_time = time.perf_counter()
            yield
            end_time = time.perf_counter()
        # TODO Also record on error
        m_process_duration.record(end_time - start_time, attrs)

    return handler_decorator_from_wrapper(call_tracer)
