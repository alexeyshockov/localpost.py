"""Shared scenario definitions: identical contracts every stack must implement.

Three scenarios for v1:

* ``plaintext``  — ``GET /ping``                  → ``b"pong"``, text/plain.
* ``path_param`` — ``GET /hello/{name}``           → ``f"hi {name}".encode()``.
* ``json_post``  — ``POST /echo`` with JSON body   → echo body verbatim, application/json.

Every app module under ``benchmarks/http/apps/`` implements these three. The
runner uses ``SCENARIOS`` to know what to fire at each stack and how to verify
the response shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    method: str
    path: str
    body: bytes | None
    content_type: str | None
    expected_status: int
    concurrency: int
    """oha -c value."""

    def url(self, base: str) -> str:
        return f"{base}{self.path}"


_JSON_BODY: Final = b'{"name":"world","numbers":[1,2,3,4,5,6,7,8,9,10]}'

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
        name="path_param",
        method="GET",
        path="/hello/world",
        body=None,
        content_type=None,
        expected_status=200,
        concurrency=64,
    ),
    Scenario(
        name="json_post",
        method="POST",
        path="/echo",
        body=_JSON_BODY,
        content_type="application/json",
        expected_status=200,
        concurrency=32,
    ),
)


# Wire-format constants every app implementation reuses.
PING_BODY: Final = b"pong"
HELLO_PREFIX: Final = "hi "
JSON_ECHO_BODY: Final = _JSON_BODY


def hello_body(name: str) -> bytes:
    return f"{HELLO_PREFIX}{name}".encode()
