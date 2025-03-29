from __future__ import annotations

import time
from contextlib import asynccontextmanager

from opentelemetry.metrics import MeterProvider, get_meter_provider
from opentelemetry.semconv._incubating.metrics.messaging_metrics import (  # noqa
    create_messaging_client_consumed_messages,
    create_messaging_client_operation_duration,
)
from opentelemetry.trace import SpanKind, TracerProvider, get_tracer_provider
from opentelemetry.util.types import AttributeValue

from localpost import __version__
from localpost.consumers.kafka import KafkaMessage
from localpost.flow import Handler, make_handler_decorator

__all__ = ["trace"]


def trace(tracer_provider: TracerProvider | None = None, meter_provider: MeterProvider | None = None, /):
    @asynccontextmanager
    async def _middleware(next_h: Handler[KafkaMessage]):
        tracer = (tracer_provider or get_tracer_provider()).get_tracer(__name__, __version__)
        meter = (meter_provider or get_meter_provider()).get_meter(__name__, __version__)

        m_process_duration = create_messaging_client_operation_duration(meter)
        messages_consumed = create_messaging_client_consumed_messages(meter)

        async def _handle(message: KafkaMessage):
            attrs: dict[str, AttributeValue] = {
                "messaging.operation.type": "process",
                "messaging.system": "kafka",
                "messaging.destination.name": message.payload.topic(),
                "messaging.kafka.partition": message.payload.partition(),
            }

            messages_consumed.add(1, attrs)
            with tracer.start_as_current_span(
                f"process {message.payload.topic()}", kind=SpanKind.CONSUMER, attributes=attrs
            ):
                start_time = time.perf_counter()
                await next_h(message)
                end_time = time.perf_counter()
            m_process_duration.record(end_time - start_time, attrs)

        yield _handle

    return make_handler_decorator(_middleware)
