"""LocalPost native handler — Router + http_server, no framework."""

from __future__ import annotations

import sys

from benchmarks.http.apps._cli import parse_port
from benchmarks.http.scenarios import PING_BODY, hello_body
from localpost.hosting import run_app
from localpost.http import RequestCtx, Response, Routes, ServerConfig, http_server


def _ping(_: RequestCtx) -> Response:
    return Response(200, {"content-type": "text/plain"}, [PING_BODY])


def _hello(ctx: RequestCtx) -> Response:
    return Response(200, {"content-type": "text/plain"}, [hello_body(ctx.path_args["name"])])


def _echo(ctx: RequestCtx) -> Response:
    return Response(200, {"content-type": "application/json"}, [ctx.body()])


def main() -> int:
    port = parse_port()
    routes = Routes()
    routes.get("/ping")(_ping)
    routes.get("/hello/{name}")(_hello)
    routes.post("/echo")(_echo)
    handler = routes.build().as_handler()
    return run_app(http_server(ServerConfig(host="127.0.0.1", port=port), handler, max_concurrency=32))


if __name__ == "__main__":
    sys.exit(main())
