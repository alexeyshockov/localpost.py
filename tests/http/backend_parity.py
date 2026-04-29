"""Parity tests across the h11 and httptools server backends.

Same handler, same canonical request set, asserted against both backends.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager

import httpx
import pytest

from localpost.http import (
    HTTPReqCtx,
    NativeResponse,
    RequestHandler,
    ServerConfig,
    start_http_server,
)
from localpost.http.server_httptools import start_httptools_server

_StartFn = Callable[[ServerConfig, RequestHandler], AbstractContextManager]

BACKENDS: list[tuple[str, _StartFn]] = [
    ("h11", start_http_server),
    ("httptools", start_httptools_server),
]


def _serve(start_fn: _StartFn, config: ServerConfig, handler: RequestHandler):
    """Background-thread server harness — yields the bound port."""

    cm = start_fn(config, handler)
    stop = threading.Event()

    class _Ctx:
        def __enter__(self) -> int:
            self._cm_state = cm.__enter__()
            server = self._cm_state

            def loop() -> None:
                while not stop.is_set():
                    try:
                        server.run(timeout=0.05)
                    except OSError:
                        return

            self._t = threading.Thread(target=loop, daemon=True)
            self._t.start()
            return server.port

        def __exit__(self, *exc):
            stop.set()
            self._t.join(timeout=5)
            cm.__exit__(*exc)

    return _Ctx()


@pytest.fixture(params=BACKENDS, ids=[b[0] for b in BACKENDS])
def serve_parity(request) -> Callable[[RequestHandler], AbstractContextManager]:
    _name, start_fn = request.param

    def make(handler: RequestHandler):
        return _serve(start_fn, ServerConfig(host="127.0.0.1", port=0), handler)

    return make


def _ok(body: bytes = b"hello"):
    def handler(ctx: HTTPReqCtx) -> None:
        ctx.complete(
            NativeResponse(
                status_code=200,
                headers=[
                    (b"content-type", b"text/plain"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            ),
            body,
        )

    return handler


def test_simple_get(serve_parity):
    with serve_parity(_ok(b"hi")) as port:
        r = httpx.get(f"http://127.0.0.1:{port}/", timeout=2)
    assert r.status_code == 200
    assert r.text == "hi"


def test_post_with_body(serve_parity):
    received: list[bytes] = []

    def handler(ctx: HTTPReqCtx) -> None:
        body = b""
        while True:
            chunk = ctx.receive(4096)
            if not chunk:
                break
            body += chunk
        received.append(body)
        out = b"echo:" + body
        ctx.complete(
            NativeResponse(
                status_code=200,
                headers=[
                    (b"content-type", b"text/plain"),
                    (b"content-length", str(len(out)).encode("ascii")),
                ],
            ),
            out,
        )

    with serve_parity(handler) as port:
        r = httpx.post(f"http://127.0.0.1:{port}/x", content=b"hello world", timeout=2)
    assert r.status_code == 200
    assert r.text == "echo:hello world"
    assert received == [b"hello world"]


@pytest.mark.parametrize("start_fn", [b for _, b in BACKENDS], ids=[n for n, _ in BACKENDS])
def test_oversize_body_returns_413(start_fn):
    """Content-Length-flagged oversize body → 413 from the parser layer."""

    def handler(ctx: HTTPReqCtx) -> None:  # never called
        ctx.complete(NativeResponse(200, [(b"content-length", b"0")]), b"")

    body = b"x" * 1024
    cm = _serve(start_fn, ServerConfig(host="127.0.0.1", port=0, max_body_size=100), handler)
    with cm as port:
        r = httpx.post(f"http://127.0.0.1:{port}/", content=body, timeout=2)
    assert r.status_code == 413


def test_keep_alive_two_requests(serve_parity):
    counter = {"n": 0}

    def handler(ctx: HTTPReqCtx) -> None:
        counter["n"] += 1
        body = str(counter["n"]).encode("ascii")
        ctx.complete(
            NativeResponse(
                200,
                [(b"content-type", b"text/plain"), (b"content-length", str(len(body)).encode("ascii"))],
            ),
            body,
        )

    with serve_parity(handler) as port:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
            s.sendall(b"GET /a HTTP/1.1\r\nHost: x\r\n\r\n")
            data1 = b""
            while b"\r\n\r\n" not in data1:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data1 += chunk
            # consume body (Content-Length: 1)
            while not data1.endswith(b"1"):
                chunk = s.recv(4096)
                if not chunk:
                    break
                data1 += chunk

            s.sendall(b"GET /b HTTP/1.1\r\nHost: x\r\n\r\n")
            data2 = b""
            while b"\r\n\r\n" not in data2:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data2 += chunk
            while not data2.endswith(b"2"):
                chunk = s.recv(4096)
                if not chunk:
                    break
                data2 += chunk

    assert b"HTTP/1.1 200" in data1
    assert data1.endswith(b"1")
    assert b"HTTP/1.1 200" in data2
    assert data2.endswith(b"2")
    assert counter["n"] == 2


def test_malformed_request_returns_400(serve_parity):
    def handler(ctx: HTTPReqCtx) -> None:  # not called
        ctx.complete(NativeResponse(200, [(b"content-length", b"0")]), b"")

    with serve_parity(handler) as port:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
            s.sendall(b"NOT-AN-HTTP-REQUEST\r\n\r\n")
            data = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
    assert b"400" in data, data


def test_expect_100_continue(serve_parity):
    def handler(ctx: HTTPReqCtx) -> None:
        body = b""
        while True:
            chunk = ctx.receive(4096)
            if not chunk:
                break
            body += chunk
        out = b"got:" + body
        ctx.complete(
            NativeResponse(
                200,
                [(b"content-type", b"text/plain"), (b"content-length", str(len(out)).encode("ascii"))],
            ),
            out,
        )

    with serve_parity(handler) as port:
        with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
            s.sendall(
                b"POST /x HTTP/1.1\r\n"
                b"Host: x\r\n"
                b"Content-Length: 5\r\n"
                b"Expect: 100-continue\r\n"
                b"\r\n"
            )
            # Read intermediate 100 Continue
            buf = b""
            s.settimeout(2)
            while b"\r\n\r\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            assert b"100" in buf, buf
            # Send body now
            s.sendall(b"hello")
            # Read final response
            while b"got:hello" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
    assert b"got:hello" in buf
