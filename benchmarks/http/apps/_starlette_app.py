"""Starlette app shared by every ASGI-side stack."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from benchmarks.http.scenarios import HELLO_PREFIX, PING_BODY


async def _ping(_: Request) -> Response:
    return Response(PING_BODY, media_type="text/plain")


async def _hello(req: Request) -> Response:
    name = req.path_params["name"]
    return Response(f"{HELLO_PREFIX}{name}".encode(), media_type="text/plain")


async def _echo(req: Request) -> Response:
    body = await req.body()
    return Response(body, media_type="application/json")


def build_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/ping", _ping, methods=["GET"]),
            Route("/hello/{name}", _hello, methods=["GET"]),
            Route("/echo", _echo, methods=["POST"]),
        ]
    )
