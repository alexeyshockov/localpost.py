"""LocalPost — ``localpost.openapi.HttpAsyncApp`` on Uvicorn.

Async sibling of ``localpost_openapi.py`` — same dataclass models, same
routes, async handlers, deployed via ``app.asgi()`` under uvicorn (1
worker). Body inputs auto-resolve via ``FromBody`` because the parameter
type is a dataclass; path/query coercion happens in the resolvers.

Like the sync flavour, LocalPost's body resolver returns ``BadRequest``
(400) on schema failure; FastAPI and flask-openapi return 422. The
``remap_validation_status`` middleware below remaps 400 → 422 on the
profile endpoint to keep the bench's ``validation_failure`` scenario
apples-to-apples.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

import uvicorn

from benchmarks.macro.openapi.apps._cli import parse_port
from localpost.openapi import (
    AsyncApiOperation,
    AsyncHTTPReqCtx,
    BadRequest,
    HttpAsyncApp,
    OpResult,
    UnprocessableEntity,
    async_op_middleware,
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


@async_op_middleware
async def remap_validation_status(
    ctx: AsyncHTTPReqCtx,
    call_next: AsyncApiOperation,
) -> UnprocessableEntity[str] | OpResult:
    result = await call_next(ctx)
    if isinstance(result, BadRequest):
        body = result.body if isinstance(result.body, str) else str(result.body)
        return UnprocessableEntity(body)
    return result


def build_app() -> HttpAsyncApp:
    app = HttpAsyncApp()

    @app.get("/ping")
    async def ping() -> str:
        return "pong"

    @app.get("/items/{item_id}")
    async def get_item(item_id: int) -> Item:
        return Item(id=item_id)

    @app.get("/search")
    async def search(q: str, limit: int = 20, offset: int = 0) -> SearchResult:
        return SearchResult(q=q, limit=limit, offset=offset)

    @app.post("/users/{user_id}/profile", middlewares=[remap_validation_status])
    async def update_profile(user_id: str, body: ProfileUpdate) -> Profile:
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
    port = parse_port()
    uvicorn.run(
        build_app().asgi(),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
