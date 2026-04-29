"""LocalPost native handler — HttpApp + httptools backend.

Same behaviour as ``localpost_native``; only the server backend differs.
"""

from __future__ import annotations

import sys
import time

from benchmarks.http.apps._cli import parse_args
from benchmarks.http.scenarios import PING_BODY, PROFILE_WORK_DELAYS_S, hello_body, profile_update_body
from localpost.hosting import run_app
from localpost.http import HttpApp, HTTPReqCtx, NativeResponse, ServerConfig


def main() -> int:
    args = parse_args()
    app = HttpApp(max_concurrency=32)

    @app.get("/ping")
    def ping():
        # Wire-bytes for the tightest plaintext path (skips the str → bytes
        # encode and the Content-Type rewrite).
        return NativeResponse(
            status_code=200,
            headers=[
                (b"content-type", b"text/plain"),
                (b"content-length", str(len(PING_BODY)).encode("ascii")),
            ],
        ), PING_BODY

    @app.get("/hello/{name}")
    def hello(name: str):
        body = hello_body(name)
        return NativeResponse(
            status_code=200,
            headers=[
                (b"content-type", b"text/plain"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        ), body

    @app.post("/echo")
    def echo(ctx: HTTPReqCtx):
        return NativeResponse(
            status_code=200,
            headers=[
                (b"content-type", b"application/json"),
                (b"content-length", str(len(ctx.body)).encode("ascii")),
            ],
        ), ctx.body

    @app.post("/users/{user_id}/profile")
    def profile_update(ctx: HTTPReqCtx, user_id: str):
        body = profile_update_body(user_id, ctx.body)
        for delay_s in PROFILE_WORK_DELAYS_S:
            time.sleep(delay_s)
        return NativeResponse(
            status_code=200,
            headers=[
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        ), body

    # Decorators are no-op-returns of the original; suppress unused warnings.
    _ = (ping, hello, echo, profile_update)

    cfg = ServerConfig(host="127.0.0.1", port=args.port)
    return run_app(app.service(cfg, backend="httptools", selectors=args.selectors))


if __name__ == "__main__":
    sys.exit(main())
