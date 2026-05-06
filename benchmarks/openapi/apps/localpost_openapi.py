"""LocalPost — ``localpost.openapi.HttpApp`` on ``localpost.http`` (h11).

Idiomatic LocalPost: dataclass models + return-type annotations. Body
inputs auto-resolve via ``FromBody`` because the parameter type is a
dataclass. Path ``int`` coercion happens in ``FromPath._cast_str``.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

from benchmarks.openapi.apps._cli import parse_port
from localpost import hosting, threadtools
from localpost.http import ServerConfig
from localpost.openapi import HttpApp


@dataclass
class Item:
    id: int


@dataclass
class ProfileUpdate:
    display_name: str
    email: str
    version: int
    tags: list[str]
    settings: dict[str, Any]


@dataclass
class Profile:
    user_id: str
    display_name: str
    email: str
    version: int
    tags: list[str]
    settings: dict[str, Any]


def build_app() -> HttpApp:
    app = HttpApp()

    @app.get("/ping")
    def ping() -> str:
        return "pong"

    @app.get("/items/{item_id}")
    def get_item(item_id: int) -> Item:
        return Item(id=item_id)

    @app.post("/users/{user_id}/profile")
    def update_profile(user_id: str, body: ProfileUpdate) -> Profile:
        tags = sorted({t.strip().lower() for t in body.tags if t.strip()})
        return Profile(
            user_id=user_id,
            display_name=body.display_name.strip(),
            email=body.email.strip().lower(),
            version=body.version + 1,
            tags=tags,
            settings=body.settings,
        )

    _ = (ping, get_item, update_profile)
    return app


def main() -> int:
    threadtools.warmup(32)
    app = build_app()
    port = parse_port()
    return hosting.run_app(app.service(ServerConfig(host="127.0.0.1", port=port, backend="h11")))


if __name__ == "__main__":
    sys.exit(main())
