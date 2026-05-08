"""LocalPost — ``localpost.openapi.HttpApp`` on ``localpost.http`` (h11).

Idiomatic LocalPost: dataclass models + return-type annotations. Body
inputs auto-resolve via ``FromBody`` because the parameter type is a
dataclass. Path/query ``int`` coercion happens in the resolvers'
``_cast_str``.

LocalPost's body resolver returns ``BadRequest`` (400) on schema
failure; FastAPI and flask-openapi return 422. To keep the bench's
``validation_failure`` scenario apples-to-apples we attach a tiny
middleware that remaps 400 → 422 on the profile endpoint.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

from benchmarks.macro.openapi.apps._cli import parse_port
from localpost import hosting, threadtools
from localpost.http import HTTPReqCtx, ServerConfig
from localpost.openapi import (
    ApiOperation,
    BadRequest,
    HttpApp,
    OpResult,
    UnprocessableEntity,
    op_middleware,
)


@dataclass
class Item:
    id: int


@dataclass
class SearchResult:
    q: str
    limit: int
    offset: int


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


@op_middleware
def remap_validation_status(
    ctx: HTTPReqCtx,
    call_next: ApiOperation,
) -> UnprocessableEntity[str] | OpResult:
    result = call_next(ctx)
    if isinstance(result, BadRequest):
        body = result.body if isinstance(result.body, str) else str(result.body)
        return UnprocessableEntity(body)
    return result


def build_app() -> HttpApp:
    app = HttpApp()

    @app.get("/ping")
    def ping() -> str:
        return "pong"

    @app.get("/items/{item_id}")
    def get_item(item_id: int) -> Item:
        return Item(id=item_id)

    @app.get("/search")
    def search(q: str, limit: int = 20, offset: int = 0) -> SearchResult:
        return SearchResult(q=q, limit=limit, offset=offset)

    @app.post("/users/{user_id}/profile", middlewares=[remap_validation_status])
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

    _ = (ping, get_item, search, update_profile)
    return app


def main() -> int:
    threadtools.warmup(32)
    app = build_app()
    port = parse_port()
    return hosting.run_app(app.service(ServerConfig(host="127.0.0.1", port=port, backend="h11")))


if __name__ == "__main__":
    sys.exit(main())
