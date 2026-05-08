"""LocalPost ``wsgi_server`` hosting the shared Flask app."""

from __future__ import annotations

import sys

from benchmarks.macro.http.apps._cli import parse_port
from benchmarks.macro.http.apps._flask_app import build_app
from localpost import threadtools
from localpost.hosting import run_app, service
from localpost.http import ServerConfig, http_server, thread_pool_handler, wrap_wsgi


def main() -> int:
    port = parse_port()
    threadtools.warmup(32)
    cfg = ServerConfig(host="127.0.0.1", port=port)

    @service
    async def app():
        async with thread_pool_handler(wrap_wsgi(build_app())) as wrapped:
            async with http_server(cfg, wrapped):
                yield

    return run_app(app())


if __name__ == "__main__":
    sys.exit(main())
