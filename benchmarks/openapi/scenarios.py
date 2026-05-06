"""Shared scenario definitions for the OpenAPI framework bench.

v1 starts with one scenario: ``plaintext`` (``GET /ping`` → ``"pong"``).
This is the calibration anchor — pure dispatch overhead, no schema, no
body. Subsequent steps add typed scenarios.

Every app module under ``benchmarks/openapi/apps/`` implements these in
its framework's idiomatic style. The runner uses ``SCENARIOS`` to know
what to fire at each stack and how to verify the response shape.
"""

from __future__ import annotations

from typing import Final

from benchmarks._core.types import Scenario

__all__ = [
    "PING_BODY",
    "SCENARIOS",
    "Scenario",
]


PING_BODY: Final = b"pong"


SCENARIOS: Final[tuple[Scenario, ...]] = (
    Scenario(
        name="plaintext",
        method="GET",
        path="/ping",
        body=None,
        content_type=None,
        expected_status=200,
        concurrency=64,
    ),
)
