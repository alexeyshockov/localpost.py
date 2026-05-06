"""LocalPost — ``localpost.openapi.HttpApp`` on ``localpost.http`` (h11)."""

from __future__ import annotations

import sys

from benchmarks.openapi.apps._cli import parse_port
from localpost import hosting, threadtools
from localpost.http import ServerConfig
from localpost.openapi import HttpApp


def build_app() -> HttpApp:
    app = HttpApp()

    @app.get("/ping")
    def ping() -> str:
        return "pong"

    _ = ping
    return app


def main() -> int:
    threadtools.warmup(32)
    app = build_app()
    port = parse_port()
    return hosting.run_app(app.service(ServerConfig(host="127.0.0.1", port=port, backend="h11")))


if __name__ == "__main__":
    sys.exit(main())
