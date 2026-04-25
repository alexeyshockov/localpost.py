"""Subprocess entry point for integration tests.

Runs a hosted localpost HTTP server (Router-based by default; switchable
to wsgi_server / flask_server via env vars) on the port given by
``LP_TEST_PORT`` and the anyio backend given by ``LP_TEST_BACKEND``
(``asyncio`` or ``trio``).
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time

import anyio


def _build_handler():
    mode = os.environ.get("LP_TEST_MODE", "router")
    if mode == "router":
        from localpost.http import RequestCtx, Response, Routes

        def _ping(_: RequestCtx) -> Response:
            return Response(200, {"content-type": "text/plain"}, [b"pong"])

        def _slow(_: RequestCtx) -> Response:
            time.sleep(float(os.environ.get("LP_TEST_SLOW_S", "0.2")))
            body = str(threading.get_ident()).encode()
            return Response(200, {"content-type": "text/plain"}, [body])

        def _hello(ctx: RequestCtx) -> Response:
            return Response(200, {"content-type": "text/plain"}, [f"hi {ctx.path_args['name']}".encode()])

        routes = Routes()
        routes.get("/ping")(_ping)
        routes.get("/slow")(_slow)
        routes.get("/hello/{name}")(_hello)
        return routes.build().as_handler()

    if mode == "wsgi":
        from localpost.http import wrap_wsgi

        def wsgi_app(environ, start_response):
            path = environ.get("PATH_INFO", "/")
            body = f"wsgi:{path}".encode()
            start_response(
                "200 OK",
                [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))],
            )
            return [body]

        return wrap_wsgi(wsgi_app)

    if mode == "flask":
        from flask import Flask

        from localpost.http.flask import flask_handler

        flask_app = Flask(__name__)

        @flask_app.route("/ping")
        def ping():
            return "pong-flask"

        @flask_app.route("/hello/<name>")
        def hello(name: str):
            return f"hi {name} (flask)"

        assert ping
        assert hello
        return flask_handler(flask_app)

    raise SystemExit(f"unknown LP_TEST_MODE: {mode!r}")


def _main() -> int:
    logging.basicConfig(level=logging.INFO)

    from localpost.hosting import run

    # Honor LP_TEST_BACKEND so tests can pin asyncio vs trio.
    backend = os.environ.get("LP_TEST_BACKEND", "asyncio")
    port = int(os.environ["LP_TEST_PORT"])
    handler = _build_handler()

    from localpost.hosting.middleware import shutdown_on_signal
    from localpost.http import ServerConfig, http_server

    cfg = ServerConfig(host="127.0.0.1", port=port)
    svc = shutdown_on_signal()(http_server(cfg, handler, max_concurrency=8))

    return anyio.run(run, svc, None, backend=backend)


if __name__ == "__main__":
    sys.exit(_main())
