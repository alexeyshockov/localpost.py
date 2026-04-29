"""LocalPost native handler — Router + http_server, no framework."""

from __future__ import annotations

import sys
import time

from benchmarks.http.apps._cli import parse_args
from benchmarks.http.scenarios import PING_BODY, PROFILE_WORK_DELAYS_S, hello_body, profile_update_body
from localpost.hosting import run_app, service
from localpost.http import (
    BodyHandler,
    HTTPReqCtx,
    NativeResponse,
    Routes,
    ServerConfig,
    http_server,
    route_match,
    thread_pool_handler,
)


def _emit(ctx: HTTPReqCtx, body: bytes, content_type: bytes = b"text/plain") -> None:
    ctx.complete(
        NativeResponse(
            status_code=200,
            headers=[(b"content-type", content_type), (b"content-length", str(len(body)).encode("ascii"))],
        ),
        body,
    )


def _ping(ctx: HTTPReqCtx) -> BodyHandler | None:
    _emit(ctx, PING_BODY)
    return None


def _hello(ctx: HTTPReqCtx) -> BodyHandler | None:
    _emit(ctx, hello_body(route_match(ctx).path_args["name"]))
    return None


def _echo_post_body(ctx: HTTPReqCtx) -> None:
    _emit(ctx, ctx.body, content_type=b"application/json")


def _echo(_: HTTPReqCtx) -> BodyHandler:
    return _echo_post_body


def _profile_update_post_body(ctx: HTTPReqCtx) -> None:
    body = profile_update_body(route_match(ctx).path_args["user_id"], ctx.body)
    for delay_s in PROFILE_WORK_DELAYS_S:
        time.sleep(delay_s)
    _emit(ctx, body, content_type=b"application/json")


def _profile_update(_: HTTPReqCtx) -> BodyHandler:
    return _profile_update_post_body


def main() -> int:
    args = parse_args()
    routes = Routes()
    routes.get("/ping")(_ping)
    routes.get("/hello/{name}")(_hello)
    routes.post("/echo")(_echo)
    routes.post("/users/{user_id}/profile")(_profile_update)
    handler = routes.build().as_handler()
    cfg = ServerConfig(host="127.0.0.1", port=args.port)

    @service
    async def app():
        async with thread_pool_handler(handler, max_concurrency=32) as wrapped:
            async with http_server(cfg, wrapped, selectors=args.selectors):
                yield

    return run_app(app())


if __name__ == "__main__":
    sys.exit(main())
