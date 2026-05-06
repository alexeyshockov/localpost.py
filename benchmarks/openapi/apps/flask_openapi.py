"""flask-openapi3 served by Gunicorn (1 worker, gthread, 32 threads)."""

from __future__ import annotations

import sys

from flask import Response
from flask_openapi import OpenAPI
from gunicorn.app.base import BaseApplication

from benchmarks.openapi.apps._cli import parse_port


def build_app() -> OpenAPI:
    app = OpenAPI(__name__)

    @app.get("/ping")
    def ping() -> Response:
        return Response(b"pong", mimetype="text/plain")

    _ = ping
    return app


class _GunicornApp(BaseApplication):
    def __init__(self, app, options: dict):
        self._app = app
        self._options = options
        super().__init__()

    def load_config(self) -> None:
        for k, v in self._options.items():
            self.cfg.set(k, v)

    def load(self):
        return self._app


def main() -> int:
    port = parse_port()
    options = {
        "bind": f"127.0.0.1:{port}",
        "workers": 1,
        "threads": 32,
        "worker_class": "gthread",
        "accesslog": None,
        "errorlog": "-",
        "loglevel": "warning",
    }
    _GunicornApp(build_app(), options).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
