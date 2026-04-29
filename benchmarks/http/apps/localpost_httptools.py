"""LocalPost native handler — Router + httptools_server (C parser).

Same handler set as ``localpost_native``; only the server backend differs.
"""

from __future__ import annotations

import sys
import time

from benchmarks.http.apps._cli import parse_port
from benchmarks.http.scenarios import PING_BODY, PROFILE_WORK_DELAYS_S, hello_body, profile_update_body
from localpost.hosting import run_app, service
from localpost.http import (
    RequestCtx,
    Response,
    Routes,
    ServerConfig,
    httptools_server,
    thread_pool_handler,
)


def _ping(_: RequestCtx) -> Response:
    return Response(200, {"content-type": "text/plain"}, [PING_BODY])


def _hello(ctx: RequestCtx) -> Response:
    return Response(200, {"content-type": "text/plain"}, [hello_body(ctx.path_args["name"])])


def _echo(ctx: RequestCtx) -> Response:
    return Response(200, {"content-type": "application/json"}, [ctx.body()])


def _profile_update(ctx: RequestCtx) -> Response:
    body = profile_update_body(ctx.path_args["user_id"], ctx.body())
    for delay_s in PROFILE_WORK_DELAYS_S:
        time.sleep(delay_s)
    return Response(200, {"content-type": "application/json"}, [body])


def main() -> int:
    port = parse_port()
    routes = Routes()
    routes.get("/ping")(_ping)
    routes.get("/hello/{name}")(_hello)
    routes.post("/echo")(_echo)
    routes.post("/users/{user_id}/profile")(_profile_update)
    handler = routes.build().as_handler()
    cfg = ServerConfig(host="127.0.0.1", port=port)

    @service
    async def app():
        async with thread_pool_handler(handler, max_concurrency=32) as wrapped:
            async with httptools_server(cfg, wrapped):
                yield

    return run_app(app())


if __name__ == "__main__":
    sys.exit(main())
