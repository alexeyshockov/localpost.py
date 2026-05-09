"""Basic-auth middleware example.

Demonstrates a :data:`localpost.http.Middleware` that validates the
``Authorization: Basic …`` header before dispatching to the inner handler.
Unauthenticated requests get a 401 on the selector thread — no worker hop.

Run::

    uv run examples/http/middleware_basic_auth.py

    curl http://localhost:8000/hello         # 401
    curl -u alice:secret http://localhost:8000/hello   # 200
"""

from __future__ import annotations

import base64
import logging
import sys

from localpost.hosting import run_app, service
from localpost.http import (
    HTTPReqCtx,
    RequestHandler,
    Response,
    Routes,
    ServerConfig,
    compose,
    http_server,
    route_match,
    thread_pool_handler,
)
from localpost.threadtools import WorkerExecutor

# ----------- credentials store (replace with a real check) ----------------

_USERS: dict[str, str] = {"alice": "secret", "bob": "pass"}


def _check_basic_auth(authorization: bytes | None) -> bool:
    if not authorization or not authorization.startswith(b"Basic "):
        return False
    try:
        decoded = base64.b64decode(authorization[6:]).decode("latin-1")
        username, _, password = decoded.partition(":")
        return _USERS.get(username) == password
    except Exception:  # noqa: BLE001
        return False


# ----------- middleware ---------------------------------------------------

_UNAUTHORIZED = Response(
    status_code=401,
    headers=[(b"www-authenticate", b'Basic realm="localpost"'), (b"content-length", b"0")],
)


def basic_auth(inner: RequestHandler) -> RequestHandler:
    """Reject requests without valid Basic credentials before dispatching."""

    def wrapped(ctx: HTTPReqCtx) -> None:
        auth_header = next(
            (v for k, v in ctx.request.headers if k == b"authorization"),
            None,
        )
        if not _check_basic_auth(auth_header):
            ctx.complete(_UNAUTHORIZED, b"")
            return
        inner(ctx)

    return wrapped


# ----------- routes -------------------------------------------------------


def _hello(ctx: HTTPReqCtx) -> None:
    name = route_match(ctx).path_args.get("name", "world")
    body = f"Hello, {name}!\n".encode()
    ctx.complete(
        Response(
            status_code=200,
            headers=[(b"content-type", b"text/plain"), (b"content-length", str(len(body)).encode())],
        ),
        body,
    )


def build_router() -> RequestHandler:
    routes = Routes()
    routes.get("/hello")(_hello)
    routes.get("/hello/{name}")(_hello)
    handler = routes.build().as_handler()
    return compose(basic_auth)(handler)


# ----------- app ----------------------------------------------------------


@service
async def app():
    config = ServerConfig(host="127.0.0.1", port=8000)
    with WorkerExecutor() as ex:
        async with thread_pool_handler(build_router(), ex) as h:
            async with http_server(config, h):
                yield


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    return run_app(app())


if __name__ == "__main__":
    sys.exit(main())
