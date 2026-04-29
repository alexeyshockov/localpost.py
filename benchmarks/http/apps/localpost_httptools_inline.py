"""LocalPost httptools backend — inline (no thread pool).

Runs every request directly on the selector thread; bypasses
``thread_pool_handler`` to isolate selector-level throughput. Used to
characterise whether multi-selector unlocks parallelism when the worker
pool isn't the bottleneck.

Reads the same ``--port`` / ``--selectors`` flags as the standard
``localpost_httptools`` app.
"""

from __future__ import annotations

import sys
import time

from benchmarks.http.apps._cli import parse_args
from benchmarks.http.scenarios import PING_BODY, PROFILE_WORK_DELAYS_S, hello_body, profile_update_body
from localpost.hosting import run_app, service
from localpost.http import (
    RequestCtx,
    Response,
    Routes,
    ServerConfig,
    httptools_server,
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
        async with httptools_server(cfg, handler, selectors=args.selectors):
            yield

    return run_app(app())


if __name__ == "__main__":
    sys.exit(main())
