"""FastAPI served by Uvicorn (1 worker)."""

from __future__ import annotations

import sys

import uvicorn
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from benchmarks.openapi.apps._cli import parse_port


def build_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)

    @app.get("/ping", response_class=PlainTextResponse)
    def ping() -> str:
        return "pong"

    _ = ping
    return app


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
