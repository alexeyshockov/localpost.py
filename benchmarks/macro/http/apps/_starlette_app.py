"""Starlette app shared by every ASGI-side stack."""

from __future__ import annotations

import asyncio

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from benchmarks.macro.http.scenarios import HELLO_PREFIX, PING_BODY, PROFILE_WORK_DELAYS_S, build_profile_update


async def _ping(_: Request) -> Response:
    return Response(PING_BODY, media_type="text/plain")


async def _hello(req: Request) -> Response:
    name = req.path_params["name"]
    return Response(f"{HELLO_PREFIX}{name}".encode(), media_type="text/plain")


async def _echo(req: Request) -> Response:
    body = await req.body()
    return Response(body, media_type="application/json")


async def _profile_update(req: Request) -> Response:
    response = build_profile_update(req.path_params["user_id"], await req.json())
    for delay_s in PROFILE_WORK_DELAYS_S:
        await asyncio.sleep(delay_s)
    return JSONResponse(response)


def build_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/ping", _ping, methods=["GET"]),
            Route("/hello/{name}", _hello, methods=["GET"]),
            Route("/echo", _echo, methods=["POST"]),
            Route("/users/{user_id}/profile", _profile_update, methods=["POST"]),
        ]
    )
