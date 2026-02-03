#!/usr/bin/env python

import os

from localpost import flow
from localpost.consumers import sqs_otel
from localpost.consumers.sqs import SqsMessage, sqs_queue_consumer


@sqs_queue_consumer("weather-forecasts")
@sqs_otel.trace()
@flow.handler
async def handle_message(message: SqsMessage):
    import random

    import anyio

    with message:
        print(f"Message received: {message.body}")
        await anyio.sleep(random.uniform(0.01, 1.5))  # Simulate some work


def configure_otel():
    from opentelemetry import metrics, trace
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    trace_provider = TracerProvider()
    processor = BatchSpanProcessor(OTLPSpanExporter())
    trace_provider.add_span_processor(processor)
    trace.set_tracer_provider(trace_provider)

    reader = PeriodicExportingMetricReader(OTLPMetricExporter())
    meter_provider = MeterProvider(metric_readers=[reader])
    metrics.set_meter_provider(meter_provider)


if __name__ == "__main__":
    import logging

    import localpost

    os.environ["AWS_ENDPOINT_URL"] = "http://127.0.0.1:4566"  # Localstack
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
    os.environ["AWS_ACCESS_KEY_ID"] = "test"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "test"
    os.environ["OTEL_SERVICE_NAME"] = "sample_sqs_consumer"
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://127.0.0.1:18889"  # Aspire Dashboard
    os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] = "grpc"

    logging.basicConfig()
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("localpost").setLevel(logging.DEBUG)

    exit(localpost.run(handle_message))
