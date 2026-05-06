"""LocalPost — ``localpost.openapi.HttpAsyncApp`` on Granian (RSGI).

Granian-flavoured sibling of ``localpost_openapi_async.py`` — same
dataclass models, same routes, same async handlers, but deployed via
``app.as_rsgi()`` under Granian's native RSGI interface (1 worker for
fair comparison with the FastAPI / uvicorn stack).

The RSGI bridge is the wire-format-only layer in
``localpost.http.rsgi``: single eager ``proto.response_bytes`` per
response (vs ASGI's two-event start+body), so the same handler chain
can be a touch faster than its ASGI sibling. This bench app exists to
quantify that on the macro-bench scenarios.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

from granian.constants import Interfaces
from granian.server import Server

from benchmarks.openapi.apps._cli import parse_port
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


# Module-level RSGI app. Granian's production ``Server`` takes the
# target as a ``module:attribute`` string and imports it inside each
# worker; ``rsgi_app`` is what it pulls.
rsgi_app = build_app().as_rsgi()


def main() -> int:
    port = parse_port()
    server = Server(
        target=f"{__name__}:rsgi_app",
        address="127.0.0.1",
        port=port,
        interface=Interfaces.RSGI,
        workers=1,
        log_enabled=False,
        log_access=False,
    )
    server.serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
