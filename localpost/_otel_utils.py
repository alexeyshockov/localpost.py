import time
from contextlib import contextmanager

from opentelemetry.metrics import Histogram
from opentelemetry.util.types import AttributeValue


@contextmanager
def rec_duration(h: Histogram, attrs: dict[str, AttributeValue]):
    start_time = time.perf_counter()
    try:
        yield
    finally:
        end_time = time.perf_counter()
        h.record(end_time - start_time, attrs)
