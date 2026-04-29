"""Diagnostic: inline httptools app that counts requests per selector thread.

Each connection is owned by exactly one ``BaseServer`` (the one whose
selector thread accepted it), and inline mode runs the handler on the
selector thread. So ``threading.get_ident()`` inside the handler maps
1:1 to "which selector served this request" — letting us inspect whether
``SO_REUSEPORT`` is actually distributing across the N selector threads.

On shutdown (SIGTERM/SIGINT) prints a histogram to stderr, e.g.::

    selector tid=6175285248: 12,341 reqs (32.1%)
    selector tid=6175821824: 13,012 reqs (33.9%)
    selector tid=6176358400: 13,041 reqs (34.0%)

A flat distribution → REUSEPORT is balancing. A skewed one → it isn't,
and selectors past the first are starved.
"""

from __future__ import annotations

import atexit
import signal
import sys
import threading
import time
from collections import Counter

from benchmarks.http.apps._cli import parse_args
from benchmarks.http.scenarios import PING_BODY, PROFILE_WORK_DELAYS_S, hello_body, profile_update_body
from localpost.hosting import run_app, service
from localpost.http import (
    BodyHandler,
    HTTPReqCtx,
    NativeResponse,
    Routes,
    ServerConfig,
    httptools_server,
    route_match,
)

_per_selector: Counter[int] = Counter()
_lock = threading.Lock()


def _record() -> None:
    tid = threading.get_ident()
    with _lock:
        _per_selector[tid] += 1


def _emit(ctx: HTTPReqCtx, body: bytes, content_type: bytes = b"text/plain") -> None:
    ctx.complete(
        NativeResponse(
            status_code=200,
            headers=[(b"content-type", content_type), (b"content-length", str(len(body)).encode("ascii"))],
        ),
        body,
    )


def _ping(ctx: HTTPReqCtx) -> BodyHandler | None:
    _record()
    _emit(ctx, PING_BODY)
    return None


def _hello(ctx: HTTPReqCtx) -> BodyHandler | None:
    _record()
    _emit(ctx, hello_body(route_match(ctx).path_args["name"]))
    return None


def _echo_post_body(ctx: HTTPReqCtx) -> None:
    _emit(ctx, ctx.body, content_type=b"application/json")


def _echo(_: HTTPReqCtx) -> BodyHandler:
    _record()
    return _echo_post_body


def _profile_update_post_body(ctx: HTTPReqCtx) -> None:
    body = profile_update_body(route_match(ctx).path_args["user_id"], ctx.body)
    for delay_s in PROFILE_WORK_DELAYS_S:
        time.sleep(delay_s)
    _emit(ctx, body, content_type=b"application/json")


def _profile_update(_: HTTPReqCtx) -> BodyHandler:
    _record()
    return _profile_update_post_body


def _print_distribution() -> None:
    with _lock:
        snapshot = dict(_per_selector)
    if not snapshot:
        return
    total = sum(snapshot.values())
    print(f"\n=== selector distribution (total={total:,}) ===", file=sys.stderr)
    for tid, n in sorted(snapshot.items(), key=lambda kv: -kv[1]):
        pct = 100.0 * n / total if total else 0.0
        print(f"  tid={tid}: {n:,} reqs ({pct:.1f}%)", file=sys.stderr)


def main() -> int:
    args = parse_args()
    routes = Routes()
    routes.get("/ping")(_ping)
    routes.get("/hello/{name}")(_hello)
    routes.post("/echo")(_echo)
    routes.post("/users/{user_id}/profile")(_profile_update)
    handler = routes.build().as_handler()
    cfg = ServerConfig(host="127.0.0.1", port=args.port)

    atexit.register(_print_distribution)
    signal.signal(signal.SIGTERM, lambda *_: (_print_distribution(), sys.exit(0)))

    @service
    async def app():
        async with httptools_server(cfg, handler, selectors=args.selectors):
            yield

    return run_app(app())


if __name__ == "__main__":
    sys.exit(main())
