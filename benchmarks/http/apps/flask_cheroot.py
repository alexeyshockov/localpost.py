"""Flask app served by Cheroot's WSGI server."""

from __future__ import annotations

import sys

from cheroot.wsgi import Server as WSGIServer

from benchmarks.http.apps._cli import parse_port
from benchmarks.http.apps._flask_app import build_app


def main() -> int:
    port = parse_port()
    server = WSGIServer(("127.0.0.1", port), build_app(), numthreads=32)
    try:
        server.start()
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
