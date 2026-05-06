"""Shared scenario definitions for the OpenAPI framework bench.

Each scenario defines one wire contract every stack must implement —
identical request shape, identical response shape — so the comparison
measures the framework's typed-handler overhead, not differences in
business logic.

The body for ``body_roundtrip`` mirrors
``benchmarks/http/scenarios.py::_PROFILE_UPDATE_BODY`` so the http and
openapi suites can be cross-referenced.
"""

from __future__ import annotations

from typing import Final

from benchmarks._core.types import Scenario

__all__ = [
    "PING_BODY",
    "PROFILE_UPDATE_BODY",
    "SCENARIOS",
    "Scenario",
]


PING_BODY: Final = b"pong"

# Same payload as ``benchmarks/http/scenarios.py``: each app must accept
# untrimmed strings, mixed-case email, duplicated/whitespaced tags, and
# return a normalized profile (trimmed, lower-cased, deduped, sorted,
# version+1).
PROFILE_UPDATE_BODY: Final = (
    b'{"display_name":" Alex Example ","email":"ALEX@example.COM","version":7,'
    b'"tags":["Python","localpost","Python"," benchmarks "],'
    b'"settings":{"theme":"dark","newsletter":true}}'
)


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
    Scenario(
        name="path_param_typed",
        method="GET",
        path="/items/42",
        body=None,
        content_type=None,
        expected_status=200,
        concurrency=64,
    ),
    Scenario(
        name="body_roundtrip",
        method="POST",
        path="/users/42/profile",
        body=PROFILE_UPDATE_BODY,
        content_type="application/json",
        expected_status=200,
        concurrency=32,
    ),
)
