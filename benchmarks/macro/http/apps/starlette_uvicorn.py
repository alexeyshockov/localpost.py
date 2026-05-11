"""Starlette app served by Uvicorn."""

from __future__ import annotations

import sys

import uvicorn

from benchmarks.macro.http.apps._cli import parse_port
from benchmarks.macro.http.apps._starlette_app import build_app


def main() -> int:
    port = parse_port()
    uvicorn.run(
        build_app(),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
