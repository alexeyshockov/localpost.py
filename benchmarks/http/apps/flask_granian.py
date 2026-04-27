"""Flask app served by Granian (WSGI mode, 1 worker, 32 blocking threads)."""

from __future__ import annotations

import sys

from granian import Granian
from granian.constants import Interfaces

from benchmarks.http.apps._cli import parse_port
from benchmarks.http.apps._flask_app import build_app

# Granian re-imports this module in each worker; the worker uses ``app`` directly.
app = build_app()


def main() -> int:
    port = parse_port()
    Granian(
        target=f"{__name__}:app",
        address="127.0.0.1",
        port=port,
        interface=Interfaces.WSGI,
        workers=1,
        blocking_threads=32,
        log_enabled=False,
    ).serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
