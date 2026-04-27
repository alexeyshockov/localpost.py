"""Shared scenario definitions: identical contracts every stack must implement.

Four scenarios for v1:

* ``plaintext``  — ``GET /ping``                  → ``b"pong"``, text/plain.
* ``path_param`` — ``GET /hello/{name}``           → ``f"hi {name}".encode()``.
* ``json_post``  — ``POST /echo`` with JSON body   → echo body verbatim, application/json.
* ``profile_update`` — ``POST /users/{user_id}/profile`` with JSON body
  → normalized user profile JSON.

Every app module under ``benchmarks/http/apps/`` implements these four. The
runner uses ``SCENARIOS`` to know what to fire at each stack and how to verify
the response shape.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final


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
_PROFILE_UPDATE_BODY: Final = (
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
    Scenario(
        name="profile_update",
        method="POST",
        path="/users/42/profile",
        body=_PROFILE_UPDATE_BODY,
        content_type="application/json",
        expected_status=200,
        concurrency=32,
    ),
)


# Wire-format constants every app implementation reuses.
PING_BODY: Final = b"pong"
HELLO_PREFIX: Final = "hi "
JSON_ECHO_BODY: Final = _JSON_BODY
PROFILE_WORK_DELAYS_S: Final = (0.001, 0.002, 0.001)


def hello_body(name: str) -> bytes:
    return f"{HELLO_PREFIX}{name}".encode()


def profile_update_payload(user_id: str, body: bytes) -> dict[str, Any]:
    payload = json.loads(body)
    return build_profile_update(user_id, payload)


def build_profile_update(user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    tags = {tag.strip().lower() for tag in (str(value) for value in payload.get("tags", ())) if tag.strip()}
    return {
        "user_id": user_id,
        "display_name": str(payload.get("display_name", "")).strip(),
        "email": str(payload.get("email", "")).strip().lower(),
        "version": int(payload.get("version", 0)) + 1,
        "tags": sorted(tags),
        "settings": payload.get("settings", {}),
    }


def profile_update_body(user_id: str, body: bytes) -> bytes:
    payload = profile_update_payload(user_id, body)
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
