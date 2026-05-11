"""Starlette app served by Granian (ASGI mode)."""

from __future__ import annotations

import sys

from granian import Granian
from granian.constants import Interfaces

from benchmarks.macro.http.apps._cli import parse_port
from benchmarks.macro.http.apps._starlette_app import build_app

app = build_app()


def main() -> int:
    port = parse_port()
    Granian(
        target=f"{__name__}:app",
        address="127.0.0.1",
        port=port,
        interface=Interfaces.ASGI,
        workers=1,
        log_enabled=False,
    ).serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
