"""Tests for ``localpost.http.app.HttpApp`` — the framework layer.

Covers decorator registration, parameter injection, response conversion,
middleware (app-level + per-route), and the hosted-service path.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, cast

import anyio
import httpx
import pytest
from anyio import to_thread

from localpost.hosting import ServiceLifetimeView, serve
from localpost.http import (
    BodyHandler,
    HTTPReqCtx,
    Middleware,
    RequestHandler,
    Response,
    ServerConfig,
    http_server,
    start_http_server,
)
from localpost.http.app import HttpApp
from tests.http._helpers import drain_socket

pytestmark = pytest.mark.anyio


# --- helpers -------------------------------------------------------------


@asynccontextmanager
async def _serve_app(
    app: HttpApp,
    cfg: ServerConfig,
) -> AsyncGenerator[ServiceLifetimeView]:
    async with serve(app.service(cfg)) as lt:
        yield lt


async def _wait_ready(port: int, deadline: float = 5.0) -> bool:
    def probe():
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return True
            except OSError:
                time.sleep(0.05)
        return False

    return await to_thread.run_sync(probe)


async def _get(url: str, **kw) -> httpx.Response:
    return await to_thread.run_sync(lambda: httpx.get(url, **kw))


async def _post(url: str, **kw) -> httpx.Response:
    return await to_thread.run_sync(lambda: httpx.post(url, **kw))


# --- response conversion -------------------------------------------------


class TestResponseConversion:
    async def test_str_returns_text(self, free_port):
        app = HttpApp()

        @app.get("/{name}")
        def hello(name: str):
            return f"Hello, {name}!"

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with _serve_app(app, cfg) as lt:
            await lt.started
            await _wait_ready(free_port)
            r = await _get(f"http://127.0.0.1:{free_port}/world")
            assert r.status_code == 200
            assert r.text == "Hello, world!"
            assert r.headers["content-type"] == "text/plain; charset=utf-8"
            lt.shutdown()
            await lt.stopped

    async def test_dict_returns_json(self, free_port):
        app = HttpApp()

        @app.get("/data")
        def data():
            return {"x": 1, "y": [2, 3]}

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with _serve_app(app, cfg) as lt:
            await lt.started
            await _wait_ready(free_port)
            r = await _get(f"http://127.0.0.1:{free_port}/data")
            assert r.status_code == 200
            assert r.headers["content-type"] == "application/json"
            assert r.json() == {"x": 1, "y": [2, 3]}
            lt.shutdown()
            await lt.stopped

    async def test_none_returns_204(self, free_port):
        app = HttpApp()

        @app.get("/empty")
        def empty():
            return None

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with _serve_app(app, cfg) as lt:
            await lt.started
            await _wait_ready(free_port)
            r = await _get(f"http://127.0.0.1:{free_port}/empty")
            assert r.status_code == 204
            lt.shutdown()
            await lt.stopped

    async def test_bytes_returns_octet_stream(self, free_port):
        app = HttpApp()

        @app.get("/bin")
        def bin_():
            return b"\x00\x01\x02"

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with _serve_app(app, cfg) as lt:
            await lt.started
            await _wait_ready(free_port)
            r = await _get(f"http://127.0.0.1:{free_port}/bin")
            assert r.status_code == 200
            assert r.content == b"\x00\x01\x02"
            assert r.headers["content-type"] == "application/octet-stream"
            lt.shutdown()
            await lt.stopped

    async def test_native_response_passes_through(self, free_port):
        """Handlers can drop to wire-format with Response. Body must match
        the declared Content-Length (or set 0)."""
        app = HttpApp()

        @app.get("/raw")
        def raw():
            return Response(
                status_code=418,
                headers=[(b"content-type", b"text/plain"), (b"content-length", b"0")],
            )

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with _serve_app(app, cfg) as lt:
            await lt.started
            await _wait_ready(free_port)
            r = await _get(f"http://127.0.0.1:{free_port}/raw")
            assert r.status_code == 418
            assert r.content == b""
            lt.shutdown()
            await lt.stopped


# --- param injection -----------------------------------------------------


class TestParamInjection:
    async def test_path_arg(self, free_port):
        app = HttpApp()

        @app.get("/users/{uid}")
        def show(uid: str):
            return f"u={uid}"

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with _serve_app(app, cfg) as lt:
            await lt.started
            await _wait_ready(free_port)
            r = await _get(f"http://127.0.0.1:{free_port}/users/alice")
            assert r.text == "u=alice"
            lt.shutdown()
            await lt.stopped

    async def test_ctx_inject(self, free_port):
        app = HttpApp()

        @app.post("/echo")
        def echo(ctx: HTTPReqCtx):
            return ctx.body

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with _serve_app(app, cfg) as lt:
            await lt.started
            await _wait_ready(free_port)
            r = await _post(f"http://127.0.0.1:{free_port}/echo", content=b"abcdef")
            assert r.status_code == 200
            assert r.content == b"abcdef"
            lt.shutdown()
            await lt.stopped

    async def test_ctx_and_path_arg(self, free_port):
        app = HttpApp()

        @app.post("/{name}/profile")
        def update_profile(ctx: HTTPReqCtx, name: str):
            payload = json.loads(ctx.body)
            return {"name": name, "payload": payload}

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with _serve_app(app, cfg) as lt:
            await lt.started
            await _wait_ready(free_port)
            r = await _post(
                f"http://127.0.0.1:{free_port}/alice/profile",
                json={"theme": "dark"},
            )
            assert r.status_code == 200
            assert r.json() == {"name": "alice", "payload": {"theme": "dark"}}
            lt.shutdown()
            await lt.stopped

    async def test_unresolvable_param_raises_at_registration(self):
        app = HttpApp()
        with pytest.raises(ValueError, match="cannot resolve parameter"):

            @app.get("/x")
            def bad(unresolved: str):
                return "x"


# --- middleware ---------------------------------------------------------


def _add_marker(value: str) -> Middleware:
    """Pre-body middleware: writes a marker into ctx.attrs."""

    def mw(inner: RequestHandler) -> RequestHandler:
        def wrapped(ctx: HTTPReqCtx) -> BodyHandler | None:
            ctx.attrs.setdefault("markers", []).append(value)
            return inner(ctx)

        return wrapped

    return mw


def _short_circuit_with_status(status: int, body: bytes) -> Middleware:
    def mw(inner: RequestHandler) -> RequestHandler:
        def wrapped(ctx: HTTPReqCtx) -> BodyHandler | None:
            ctx.complete(
                Response(
                    status_code=status,
                    headers=[(b"content-type", b"text/plain"), (b"content-length", str(len(body)).encode("ascii"))],
                ),
                body,
            )
            return None  # do not call inner

        return wrapped

    return mw


class TestMiddleware:
    async def test_app_level_middleware_runs(self, free_port):
        captured: list[str] = []

        def capturing_mw(inner):
            def wrapped(ctx):
                captured.append("before")
                result = inner(ctx)
                if result is None:
                    captured.append("after-inline")
                    return None

                # wrap continuation
                def post(ctx):
                    result(ctx)
                    captured.append("after-body")

                return post

            return wrapped

        app = HttpApp(middleware=[capturing_mw])

        @app.get("/{name}")
        def hello(name: str):
            return f"hi {name}"

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with _serve_app(app, cfg) as lt:
            await lt.started
            await _wait_ready(free_port)
            r = await _get(f"http://127.0.0.1:{free_port}/world")
            assert r.text == "hi world"
            lt.shutdown()
            await lt.stopped

        assert captured == ["before", "after-body"]

    async def test_app_middleware_short_circuits_pre_body(self, free_port):
        app = HttpApp(middleware=[_short_circuit_with_status(401, b"unauthorized")])

        @app.get("/{name}")
        def hello(name: str):
            return f"hi {name}"  # pragma: no cover

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with _serve_app(app, cfg) as lt:
            await lt.started
            await _wait_ready(free_port)
            r = await _get(f"http://127.0.0.1:{free_port}/world")
            assert r.status_code == 401
            assert r.text == "unauthorized"
            lt.shutdown()
            await lt.stopped

    async def test_per_route_middleware(self, free_port):
        app = HttpApp()

        @app.get("/protected", middleware=[_short_circuit_with_status(403, b"forbidden")])
        def protected():
            return "secret"  # pragma: no cover

        @app.get("/public")
        def public():
            return "open"

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with _serve_app(app, cfg) as lt:
            await lt.started
            await _wait_ready(free_port)
            r1 = await _get(f"http://127.0.0.1:{free_port}/protected")
            assert r1.status_code == 403
            r2 = await _get(f"http://127.0.0.1:{free_port}/public")
            assert r2.status_code == 200
            assert r2.text == "open"
            lt.shutdown()
            await lt.stopped

    async def test_attrs_propagate_pre_to_post_body(self, free_port):
        """Middleware writes to ctx.attrs in pre-body; post-body handler reads it."""
        app = HttpApp(middleware=[_add_marker("auth-ok")])

        @app.post("/echo")
        def echo(ctx: HTTPReqCtx):
            return {
                "markers": ctx.attrs.get("markers", []),
                "body": ctx.body.decode("utf-8"),
            }

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with _serve_app(app, cfg) as lt:
            await lt.started
            await _wait_ready(free_port)
            r = await _post(f"http://127.0.0.1:{free_port}/echo", content=b"hi")
            assert r.json() == {"markers": ["auth-ok"], "body": "hi"}
            lt.shutdown()
            await lt.stopped


# --- 404 / 405 -----------------------------------------------------------


class TestNotFoundAndMethodNotAllowed:
    async def test_404_when_no_route(self, free_port):
        app = HttpApp()

        @app.get("/known")
        def known():
            return "x"

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with _serve_app(app, cfg) as lt:
            await lt.started
            await _wait_ready(free_port)
            r = await _get(f"http://127.0.0.1:{free_port}/missing")
            assert r.status_code == 404
            lt.shutdown()
            await lt.stopped

    async def test_405_with_allow_header(self, free_port):
        app = HttpApp()

        @app.post("/r")
        def post_only():
            return "ok"

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with _serve_app(app, cfg) as lt:
            await lt.started
            await _wait_ready(free_port)
            r = await _get(f"http://127.0.0.1:{free_port}/r")
            assert r.status_code == 405
            assert r.headers.get("allow") == "POST"
            lt.shutdown()
            await lt.stopped


# --- pool / concurrency --------------------------------------------------


class TestWorkerPool:
    async def test_handlers_run_on_worker_threads(self, free_port):
        """Default (``pooled=True``) — handlers run on worker threads,
        not the selector thread."""
        seen: list[int] = []
        lock = threading.Lock()

        app = HttpApp()

        @app.get("/tid")
        def tid():
            with lock:
                seen.append(threading.get_ident())
            return "x"

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with _serve_app(app, cfg) as lt:
            await lt.started
            await _wait_ready(free_port)

            async with anyio.create_task_group() as tg:
                for _ in range(8):
                    tg.start_soon(_get, f"http://127.0.0.1:{free_port}/tid")

            assert len(seen) == 8
            assert len(set(seen)) >= 2  # at least two distinct worker threads

            lt.shutdown()
            await lt.stopped

    async def test_pooled_false_runs_inline(self, free_port):
        """When the pool is disabled, handlers run on the selector thread."""
        seen: set[int] = set()
        lock = threading.Lock()

        app = HttpApp(pooled=False)

        @app.get("/tid")
        def tid():
            with lock:
                seen.add(threading.get_ident())
            return "x"

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with _serve_app(app, cfg) as lt:
            await lt.started
            await _wait_ready(free_port)

            for _ in range(5):
                await _get(f"http://127.0.0.1:{free_port}/tid")

            assert len(seen) == 1  # all on the selector thread

            lt.shutdown()
            await lt.stopped


# --- backend selection ---------------------------------------------------


class TestBackendSelection:
    async def test_explicit_backend_basic_response(self, free_port, http_backend):
        app = HttpApp()

        @app.get("/{name}")
        def hello(name: str):
            return f"hi {name}"

        cfg = ServerConfig(host="127.0.0.1", port=free_port, backend=http_backend)
        async with _serve_app(app, cfg) as lt:
            await lt.started
            await _wait_ready(free_port)
            r = await _get(f"http://127.0.0.1:{free_port}/world")
            assert r.status_code == 200
            assert r.text == "hi world"
            lt.shutdown()
            await lt.stopped

    def test_invalid_backend(self):
        cfg = ServerConfig(host="127.0.0.1", port=0, backend=cast(Any, "bogus"))
        with pytest.raises(ValueError, match="unknown backend"):
            start_http_server(cfg, lambda ctx: None)


class TestStreamingRoutes:
    async def test_streaming_route_reads_body_in_handler(self, free_port, http_backend):
        """``buffer_body=False`` — handler runs in a worker on a borrowed
        conn and reads body chunks via ``ctx.receive(...)``."""
        captured: dict = {}

        app = HttpApp()

        @app.post("/upload", buffer_body=False)
        def upload(ctx: HTTPReqCtx):
            chunks: list[bytes] = []
            while True:
                chunk = ctx.receive(8192)
                if not chunk:
                    break
                chunks.append(chunk)
            full = b"".join(chunks)
            captured["body"] = full
            captured["thread"] = threading.get_ident()
            return f"got {len(full)} bytes"

        cfg = ServerConfig(host="127.0.0.1", port=free_port, backend=http_backend)
        async with _serve_app(app, cfg) as lt:
            await lt.started
            await _wait_ready(free_port)
            payload = b"a" * 1024
            r = await _post(f"http://127.0.0.1:{free_port}/upload", content=payload)
            assert r.status_code == 200
            assert r.text == "got 1024 bytes"
            lt.shutdown()
            await lt.stopped

        assert captured["body"] == b"a" * 1024
        # Streaming handler runs on a worker thread, not the main thread.
        assert captured["thread"] != threading.get_ident()

    async def test_streaming_route_oversized_chunked_body_returns_413(self, free_port, http_backend):
        app = HttpApp()

        @app.post("/upload", buffer_body=False)
        def upload(ctx: HTTPReqCtx):
            while ctx.receive(8192):
                pass
            return "ok"

        cfg = ServerConfig(host="127.0.0.1", port=free_port, max_body_size=8, backend=http_backend)
        async with _serve_app(app, cfg) as lt:
            await lt.started
            await _wait_ready(free_port)

            def hit() -> bytes:
                with socket.create_connection(("127.0.0.1", free_port), timeout=3.0) as sock:
                    sock.sendall(
                        b"POST /upload HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
                        b"10\r\n" + (b"x" * 16) + b"\r\n0\r\n\r\n"
                    )
                    return drain_socket(sock, deadline=2.0)

            response = await to_thread.run_sync(hit)
            assert b"HTTP/1.1 413" in response
            assert b"Payload Too Large" in response
            lt.shutdown()
            await lt.stopped

    async def test_streaming_route_body_across_multiple_recvs_httptools(self, free_port):
        """The client sends headers in one TCP write, body in subsequent
        writes (so the parser only sees headers in the selector's first
        feed). The worker must pull the rest through the parser via
        ``ctx.receive`` → ``sock.recv`` → ``feed_data`` → ``on_body``."""
        captured: dict = {}

        app = HttpApp()

        @app.post("/upload", buffer_body=False)
        def upload(ctx: HTTPReqCtx):
            chunks: list[bytes] = []
            while True:
                chunk = ctx.receive(8192)
                if not chunk:
                    break
                chunks.append(chunk)
            captured["body"] = b"".join(chunks)
            return f"got {len(captured['body'])} bytes"

        cfg = ServerConfig(host="127.0.0.1", port=free_port, backend="httptools")
        async with serve(app.service(cfg)) as lt:
            await lt.started
            await _wait_ready(free_port)

            def hit() -> bytes:
                payload = b"a" * 4096
                with socket.create_connection(("127.0.0.1", free_port), timeout=3.0) as s:
                    s.sendall(b"POST /upload HTTP/1.1\r\nHost: x\r\nContent-Length: 4096\r\n\r\n")
                    # Send body in three smaller writes with brief pauses;
                    # the parser will see only headers in its first feed.
                    for i in (0, 1024, 2048):
                        time.sleep(0.05)
                        s.sendall(payload[i : i + 1024])
                    time.sleep(0.05)
                    s.sendall(payload[3072:4096])
                    buf = b""
                    while b"got 4096 bytes" not in buf:
                        chunk = s.recv(4096)
                        if not chunk:
                            break
                        buf += chunk
                    return buf

            response = await to_thread.run_sync(hit)
            assert b"HTTP/1.1 200" in response
            assert b"got 4096 bytes" in response
            lt.shutdown()
            await lt.stopped

        assert captured["body"] == b"a" * 4096

    async def test_httptools_deferred_streaming_dispatch_sees_current_feed_body(self, free_port):
        """httptools starts streaming work after callbacks from the current feed finish."""
        captured: dict = {}

        def handler(ctx: HTTPReqCtx):
            def dispatch(req_ctx: HTTPReqCtx) -> None:
                conn = cast(Any, req_ctx).conn
                conn.selector.stop_tracking(conn)
                captured["buffer_before_receive"] = bytes(conn._streaming_body_buf)
                captured["eom_before_receive"] = conn._streaming_eom
                chunks: list[bytes] = []
                while True:
                    chunk = req_ctx.receive(8192)
                    if not chunk:
                        break
                    chunks.append(chunk)
                captured["body"] = b"".join(chunks)
                req_ctx.complete(Response(200, [(b"content-length", b"2")]), b"ok")

            cast(Any, ctx)._defer_streaming_dispatch(dispatch)

        cfg = ServerConfig(host="127.0.0.1", port=free_port, backend="httptools")
        async with serve(http_server(cfg, handler)) as lt:
            await lt.started
            await _wait_ready(free_port)

            def hit() -> bytes:
                payload = b"a" * 1024
                with socket.create_connection(("127.0.0.1", free_port), timeout=3.0) as s:
                    s.sendall(b"POST /upload HTTP/1.1\r\nHost: x\r\nContent-Length: 1024\r\n\r\n" + payload)
                    buf = b""
                    while b"\r\n\r\nok" not in buf:
                        chunk = s.recv(4096)
                        if not chunk:
                            break
                        buf += chunk
                    return buf

            response = await to_thread.run_sync(hit)
            assert b"HTTP/1.1 200" in response
            assert b"\r\n\r\nok" in response
            lt.shutdown()
            await lt.stopped

        assert captured == {
            "buffer_before_receive": b"a" * 1024,
            "eom_before_receive": True,
            "body": b"a" * 1024,
        }

    async def test_streaming_then_keep_alive_request_httptools(self, free_port):
        """After a streaming POST, the same connection serves a regular
        GET. Verifies parser state is consistent after streaming — the
        next request parses cleanly without any reset / replace."""
        captured: list[str] = []

        app = HttpApp()

        @app.post("/upload", buffer_body=False)
        def upload(ctx: HTTPReqCtx):
            total = 0
            while True:
                chunk = ctx.receive(8192)
                if not chunk:
                    break
                total += len(chunk)
            captured.append(f"upload:{total}")
            return f"got {total} bytes"

        @app.get("/ping")
        def ping():
            captured.append("ping")
            return "pong"

        cfg = ServerConfig(host="127.0.0.1", port=free_port, backend="httptools")
        async with serve(app.service(cfg)) as lt:
            await lt.started
            await _wait_ready(free_port)

            def hit() -> bytes:
                with socket.create_connection(("127.0.0.1", free_port), timeout=3.0) as s:
                    # Streaming POST.
                    s.sendall(b"POST /upload HTTP/1.1\r\nHost: x\r\nContent-Length: 1024\r\n\r\n" + b"a" * 1024)
                    buf1 = b""
                    while b"got 1024 bytes" not in buf1:
                        chunk = s.recv(4096)
                        if not chunk:
                            break
                        buf1 += chunk
                    # Now reuse the same conn for a regular GET — would fail
                    # if streaming had left the parser in a stale state.
                    s.sendall(b"GET /ping HTTP/1.1\r\nHost: x\r\n\r\n")
                    buf2 = b""
                    while b"pong" not in buf2:
                        chunk = s.recv(4096)
                        if not chunk:
                            break
                        buf2 += chunk
                    return buf1 + buf2

            response = await to_thread.run_sync(hit)
            assert b"got 1024 bytes" in response
            assert b"pong" in response
            lt.shutdown()
            await lt.stopped

        assert captured == ["upload:1024", "ping"]

    def test_streaming_route_with_pool_disabled_raises(self):
        """Registering a streaming route on an HttpApp with ``pooled=False``
        raises ``RuntimeError`` when the dispatcher is built."""
        app = HttpApp(pooled=False)

        @app.post("/upload", buffer_body=False)
        def upload(ctx: HTTPReqCtx):  # pragma: no cover
            return "x"

        assert upload is not None
        # Buffered routes work fine without a pool, but streaming ones don't.
        with pytest.raises(RuntimeError, match="streaming route"):
            app._build_router_handler(None)


# Avoid "imported but unused" lints — the helper is part of the public smoke API.
_keep = (contextlib,)
