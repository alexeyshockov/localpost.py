"""Tests for ``localpost.http.wsgi.to_wsgi`` — serving a localpost
``RequestHandler`` under a WSGI server.

Drives the WSGI app directly (no real WSGI server needed); asserts on the
captured ``start_response`` arguments and the returned body iterable.
"""

from __future__ import annotations

import io
from collections.abc import Iterable, Iterator
from typing import Any

import pytest

from localpost.http import (
    HTTPReqCtx,
    Response,
    Routes,
    to_wsgi,
)

# --------------------------------------------------------------------------
# Helpers — drive the WSGI app and capture the response.
# --------------------------------------------------------------------------


def _base_environ(
    *,
    method: str = "GET",
    path: str = "/",
    query: str = "",
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    server: tuple[str, str] = ("localhost", "8000"),
    client: tuple[str, str] | None = ("203.0.113.4", "54321"),
    scheme: str = "http",
    file_wrapper: bool = False,
) -> dict[str, Any]:
    environ: dict[str, Any] = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "SERVER_NAME": server[0],
        "SERVER_PORT": server[1],
        "SERVER_PROTOCOL": "HTTP/1.1",
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": scheme,
        "wsgi.input": io.BytesIO(body),
        "wsgi.errors": io.StringIO(),
        "wsgi.multithread": True,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
        "CONTENT_LENGTH": str(len(body)) if body else "",
        "CONTENT_TYPE": "",
    }
    if client is not None:
        environ["REMOTE_ADDR"] = client[0]
        environ["REMOTE_PORT"] = client[1]
    if file_wrapper:
        environ["wsgi.file_wrapper"] = lambda f, blksize=8192: _IterFile(f, blksize)
    if headers:
        for k, v in headers.items():
            if k.lower() == "content-type":
                environ["CONTENT_TYPE"] = v
            elif k.lower() == "content-length":
                environ["CONTENT_LENGTH"] = v
            else:
                environ["HTTP_" + k.upper().replace("-", "_")] = v
    return environ


class _IterFile:
    """Minimal ``wsgi.file_wrapper`` stand-in — reads in ``blksize`` chunks."""

    def __init__(self, fileobj: Any, blksize: int) -> None:
        self._f = fileobj
        self._blk = blksize

    def __iter__(self) -> Iterator[bytes]:
        while True:
            chunk = self._f.read(self._blk)
            if not chunk:
                return
            yield chunk


def _drive(app: Any, environ: dict[str, Any]) -> tuple[str, list[tuple[str, str]], bytes]:
    """Invoke the WSGI app once. Returns (status_line, headers, body)."""
    captured: dict[str, Any] = {}

    def start_response(status: str, headers: list[tuple[str, str]], exc_info: Any = None) -> Any:
        captured["status"] = status
        captured["headers"] = headers
        return lambda _: None

    body_iter: Iterable[bytes] = app(environ, start_response)
    body = b"".join(body_iter)
    close = getattr(body_iter, "close", None)
    if close is not None:
        close()
    return captured["status"], captured["headers"], body


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


class TestToWsgiComplete:
    def test_simple_get(self):
        def handler(ctx: HTTPReqCtx):
            ctx.complete(
                Response(200, [(b"content-type", b"text/plain"), (b"content-length", b"5")]),
                b"hello",
            )

        status, headers, body = _drive(to_wsgi(handler), _base_environ())
        assert status.startswith("200 ")
        assert ("content-type", "text/plain") in headers
        assert body == b"hello"

    def test_204_no_body(self):
        def handler(ctx: HTTPReqCtx):
            ctx.complete(Response(204, [(b"content-length", b"0")]))

        status, _headers, body = _drive(to_wsgi(handler), _base_environ())
        assert status.startswith("204 ")
        assert body == b""

    def test_request_method_path_query(self):
        seen: dict[str, Any] = {}

        def handler(ctx: HTTPReqCtx):
            seen["method"] = ctx.request.method
            seen["path"] = ctx.request.path
            seen["query"] = ctx.request.query_string
            ctx.complete(Response(200, [(b"content-length", b"0")]), b"")

        environ = _base_environ(method="POST", path="/items/42", query="x=1&y=2")
        _drive(to_wsgi(handler), environ)
        assert seen["method"] == b"POST"
        assert seen["path"] == b"/items/42"
        assert seen["query"] == b"x=1&y=2"

    def test_request_body_buffered(self):
        seen: dict[str, bytes] = {}

        def handler(ctx: HTTPReqCtx):
            seen["body"] = ctx.body
            ctx.complete(Response(200, [(b"content-length", b"0")]), b"")

        environ = _base_environ(
            method="POST",
            body=b'{"hello":"world"}',
            headers={"Content-Type": "application/json"},
        )
        _drive(to_wsgi(handler), environ)
        assert seen["body"] == b'{"hello":"world"}'

    def test_headers_lowercased_and_passed_through(self):
        seen: dict[str, Any] = {}

        def handler(ctx: HTTPReqCtx):
            seen["headers"] = dict(ctx.request.headers)
            ctx.complete(Response(200, [(b"content-length", b"0")]), b"")

        environ = _base_environ(
            headers={
                "Content-Type": "application/json",
                "Content-Length": "0",
                "Authorization": "Bearer abc",
                "X-Trace-Id": "t-1",
            }
        )
        _drive(to_wsgi(handler), environ)
        h = seen["headers"]
        assert h[b"content-type"] == b"application/json"
        assert h[b"authorization"] == b"Bearer abc"
        assert h[b"x-trace-id"] == b"t-1"

    def test_remote_addr_local_addr_scheme(self):
        seen: dict[str, Any] = {}

        def handler(ctx: HTTPReqCtx):
            seen["remote"] = ctx.remote_addr
            seen["local"] = ctx.local_addr
            seen["scheme"] = ctx.scheme
            ctx.complete(Response(200, [(b"content-length", b"0")]), b"")

        environ = _base_environ(server=("api.example.com", "443"), scheme="https")
        _drive(to_wsgi(handler), environ)
        assert seen["remote"] == "203.0.113.4:54321"
        assert seen["local"] == "api.example.com:443"
        assert seen["scheme"] == "https"

    def test_remote_addr_none_when_unknown(self):
        seen: dict[str, Any] = {}

        def handler(ctx: HTTPReqCtx):
            seen["remote"] = ctx.remote_addr
            ctx.complete(Response(200, [(b"content-length", b"0")]), b"")

        environ = _base_environ(client=None)
        _drive(to_wsgi(handler), environ)
        assert seen["remote"] is None

    def test_disconnected_always_false(self):
        seen: dict[str, Any] = {}

        def handler(ctx: HTTPReqCtx):
            seen["disconnected"] = ctx.disconnected
            ctx.complete(Response(200, [(b"content-length", b"0")]), b"")

        environ = _base_environ()
        _drive(to_wsgi(handler), environ)
        # WSGI has no socket handle — disconnects surface as BrokenPipeError
        # from the host's per-chunk write, not as a poll signal here.
        assert seen["disconnected"] is False


class TestToWsgiStream:
    def test_stream_iterator_handed_off_lazily(self):
        consumed: list[int] = []

        def gen() -> Iterator[bytes]:
            for i in range(3):
                consumed.append(i)
                yield f"chunk-{i}\n".encode()

        def handler(ctx: HTTPReqCtx):
            ctx.stream(
                Response(200, [(b"content-type", b"text/event-stream")]),
                gen(),
            )

        environ = _base_environ()
        captured: dict[str, Any] = {}

        def start_response(status: str, headers: list[tuple[str, str]], exc_info: Any = None) -> Any:
            captured["status"] = status
            captured["headers"] = headers
            return lambda _: None

        body_iter = to_wsgi(handler)(environ, start_response)
        # Lazy: nothing produced yet beyond what to_wsgi materialised.
        assert consumed == []
        # Drain: WSGI server pacing.
        chunks = list(body_iter)
        assert consumed == [0, 1, 2]
        assert b"".join(chunks) == b"chunk-0\nchunk-1\nchunk-2\n"
        assert captured["status"].startswith("200 ")


class TestToWsgiSendfile:
    def test_sendfile_with_file_wrapper(self, tmp_path):
        path = tmp_path / "f.txt"
        path.write_bytes(b"abcdefghij")

        def handler(ctx: HTTPReqCtx):
            f = path.open("rb")
            ctx.sendfile(
                Response(200, [(b"content-length", b"4")]),
                f,
                offset=2,
                count=4,
            )

        status, _headers, body = _drive(to_wsgi(handler), _base_environ(file_wrapper=True))
        assert status.startswith("200 ")
        assert body == b"cdef"

    def test_sendfile_without_file_wrapper_falls_back_to_chunks(self, tmp_path):
        path = tmp_path / "f.txt"
        path.write_bytes(b"abcdefghij")

        def handler(ctx: HTTPReqCtx):
            f = path.open("rb")
            ctx.sendfile(
                Response(200, [(b"content-length", b"5")]),
                f,
                offset=0,
                count=5,
            )

        status, _headers, body = _drive(to_wsgi(handler), _base_environ(file_wrapper=False))
        assert status.startswith("200 ")
        assert body == b"abcde"


class TestToWsgiRouter:
    def test_router_dispatch_404(self):
        routes = Routes()

        @routes.get("/known")
        def known(ctx: HTTPReqCtx):
            ctx.complete(Response(200, [(b"content-length", b"2")]), b"ok")

        app = routes.build().as_wsgi()
        status, _headers, body = _drive(app, _base_environ(path="/missing"))
        assert status.startswith("404 ")
        assert body == b"Not Found"

    def test_router_dispatch_405(self):
        routes = Routes()

        @routes.get("/x")
        def get_x(ctx: HTTPReqCtx):
            ctx.complete(Response(200, [(b"content-length", b"0")]), b"")

        app = routes.build().as_wsgi()
        status, headers, _body = _drive(app, _base_environ(method="POST", path="/x"))
        assert status.startswith("405 ")
        assert ("Allow", "GET") in headers

    def test_router_match_with_path_args(self):
        routes = Routes()

        @routes.get("/items/{id}")
        def get_item(ctx: HTTPReqCtx):
            from localpost.http.router import route_match  # noqa: PLC0415

            match = route_match(ctx)
            body = match.path_args["id"].encode()
            ctx.complete(Response(200, [(b"content-length", str(len(body)).encode())]), body)

        app = routes.build().as_wsgi()
        status, _headers, body = _drive(app, _base_environ(path="/items/42"))
        assert status.startswith("200 ")
        assert body == b"42"


class TestToWsgiBorrow:
    def test_borrowed_always_true(self):
        seen: dict[str, Any] = {}

        def handler(ctx: HTTPReqCtx):
            seen["borrowed"] = ctx.borrowed
            with ctx.borrow() as inner:
                seen["inner"] = inner is ctx
                seen["borrowed_inside"] = ctx.borrowed
            ctx.complete(Response(200, [(b"content-length", b"0")]), b"")

        _drive(to_wsgi(handler), _base_environ())
        assert seen["borrowed"] is True
        assert seen["borrowed_inside"] is True
        assert seen["inner"] is True


class TestToWsgiReceive:
    def test_receive_slices_buffered_body(self):
        chunks: list[bytes] = []

        def handler(ctx: HTTPReqCtx):
            while True:
                c = ctx.receive(4)
                if not c:
                    break
                chunks.append(c)
            ctx.complete(Response(200, [(b"content-length", b"0")]), b"")

        environ = _base_environ(method="POST", body=b"abcdefghij")
        _drive(to_wsgi(handler), environ)
        assert chunks == [b"abcd", b"efgh", b"ij"]
