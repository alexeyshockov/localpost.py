"""LocalPost ``wsgi_server`` hosting the shared Flask app."""

from __future__ import annotations

import sys

from benchmarks.http.apps._cli import parse_port
from benchmarks.http.apps._flask_app import build_app
from localpost.hosting import run_app
from localpost.http import ServerConfig, wsgi_server


def main() -> int:
    port = parse_port()
    return run_app(wsgi_server(ServerConfig(host="127.0.0.1", port=port), build_app(), max_concurrency=32))


if __name__ == "__main__":
    sys.exit(main())
