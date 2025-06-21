from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection
from contextlib import AbstractContextManager, contextmanager
from typing import Any

from opentelemetry.metrics import MeterProvider, get_meter_provider
from opentelemetry.semconv._incubating.metrics.messaging_metrics import (  # noqa
    create_messaging_client_consumed_messages,
    create_messaging_client_operation_duration,
)
from opentelemetry.trace import SpanKind, TracerProvider, get_tracer_provider
from opentelemetry.util.types import AttributeValue
from typing_extensions import TypeVar

from localpost import __version__
from localpost._otel_utils import rec_duration
from localpost.flow import FlowHandler, HandlerDecorator, handler_middleware

T = TypeVar("T", default=Any)
TC = TypeVar("TC", bound=Collection[object], default=Collection[object])
R = TypeVar("R", Awaitable[None], None)

__all__ = ["trace", "trace_batch"]


def create_message_tracer(
    queue_name: str,
    batched: bool,
    tp: TracerProvider | None,
    mp: MeterProvider | None,
) -> Callable[[T], AbstractContextManager[None]]:
    tracer = (tp or get_tracer_provider()).get_tracer(__name__, __version__)
    meter = (mp or get_meter_provider()).get_meter(__name__, __version__)

    # Based on Semantic Conventions 1.30.0, see
    # https://opentelemetry.io/docs/specs/semconv/messaging/messaging-spans/

    m_process_duration = create_messaging_client_operation_duration(meter)
    messages_consumed = create_messaging_client_consumed_messages(meter)

    @contextmanager
    def call_tracer(message: T):
        attrs: dict[str, AttributeValue] = {
            "messaging.operation.type": "process",
            "messaging.system": "localpost_streams",
            "messaging.destination.name": queue_name,
        }
        if batched:
            attrs["messaging.batch.message_count"] = len(message)

        messages_consumed.add(len(message) if batched else 1, attrs)
        with tracer.start_as_current_span(f"process {queue_name}", kind=SpanKind.CONSUMER, attributes=attrs):
            with rec_duration(m_process_duration, attrs):
                yield

    return call_tracer


def trace(
    stream_name: str, tp: TracerProvider | None = None, mp: MeterProvider | None = None, /
) -> HandlerDecorator[T, T]:
    @handler_middleware
    async def middleware(next_h: FlowHandler):
        call_tracer = create_message_tracer(stream_name, False, tp, mp)

        async def _handle_async(item):
            with call_tracer(item):
                await next_h.async_h(item)

        def _handle_sync(item):
            with call_tracer(item):
                next_h.sync_h(item)

        yield next_h.create(async_h=_handle_async, sync_h=_handle_sync)

    return middleware


def trace_batch(
    stream_name: str, tp: TracerProvider | None = None, mp: MeterProvider | None = None, /
) -> HandlerDecorator[TC, TC]:
    @handler_middleware
    async def middleware(next_h: FlowHandler):
        call_tracer = create_message_tracer(stream_name, False, tp, mp)

        async def _handle_async(item):
            with call_tracer(item):
                await next_h.async_h(item)

        def _handle_sync(item):
            with call_tracer(item):
                next_h.sync_h(item)

        yield next_h.create(async_h=_handle_async, sync_h=_handle_sync)

    return middleware
