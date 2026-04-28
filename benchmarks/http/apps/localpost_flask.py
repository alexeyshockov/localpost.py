"""LocalPost ``flask_server`` (native Flask adapter) hosting the shared Flask app."""

from __future__ import annotations

import sys

from benchmarks.http.apps._cli import parse_port
from benchmarks.http.apps._flask_app import build_app
from localpost.hosting import run_app, service
from localpost.http import ServerConfig, http_server, thread_pool_handler
from localpost.http.flask import flask_handler


def main() -> int:
    port = parse_port()
    cfg = ServerConfig(host="127.0.0.1", port=port)

    @service
    async def app():
        async with thread_pool_handler(flask_handler(build_app()), max_concurrency=32) as wrapped:
            async with http_server(cfg, wrapped):
                yield

    return run_app(app())


if __name__ == "__main__":
    sys.exit(main())
