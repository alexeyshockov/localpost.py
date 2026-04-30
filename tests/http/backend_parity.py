"""Shared HTTP behavior contract across the h11 and httptools backends."""

from __future__ import annotations

import socket

import httpx

from localpost.http import HTTPReqCtx, NativeResponse, ServerConfig
from tests.http._helpers import drain_socket, read_http_response, read_until


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


def test_simple_get(serve_backend_in_thread):
    with serve_backend_in_thread(_ok(b"hi")) as port:
        r = httpx.get(f"http://127.0.0.1:{port}/", timeout=2)
    assert r.status_code == 200
    assert r.text == "hi"


def test_buffered_post_body_handler(serve_backend_in_thread):
    received: list[bytes] = []

    def body_handler(ctx: HTTPReqCtx) -> None:
        received.append(ctx.body)
        out = b"echo:" + ctx.body
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

    def handler(_ctx: HTTPReqCtx):
        return body_handler

    with serve_backend_in_thread(handler) as port:
        r = httpx.post(f"http://127.0.0.1:{port}/x", content=b"hello world", timeout=2)
    assert r.status_code == 200
    assert r.text == "echo:hello world"
    assert received == [b"hello world"]


def test_keep_alive_sequential_requests(serve_backend_in_thread):
    counter = 0

    def handler(ctx: HTTPReqCtx) -> None:
        nonlocal counter
        counter += 1
        body = str(counter).encode("ascii")
        ctx.complete(
            NativeResponse(
                200,
                [
                    (b"content-type", b"text/plain"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            ),
            body,
        )

    with serve_backend_in_thread(handler) as port:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
            sock.sendall(b"GET /a HTTP/1.1\r\nHost: x\r\n\r\n")
            data1 = read_http_response(sock)

            sock.sendall(b"GET /b HTTP/1.1\r\nHost: x\r\n\r\n")
            data2 = read_http_response(sock)

    assert b"HTTP/1.1 200" in data1
    assert data1.endswith(b"1")
    assert b"HTTP/1.1 200" in data2
    assert data2.endswith(b"2")
    assert counter == 2


def test_malformed_request_returns_400(serve_backend_in_thread):
    def handler(ctx: HTTPReqCtx) -> None:
        ctx.complete(NativeResponse(200, [(b"content-length", b"0")]), b"")

    with serve_backend_in_thread(handler) as port:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
            sock.sendall(b"NOT-AN-HTTP-REQUEST\r\n\r\n")
            data = drain_socket(sock, deadline=1.0)
    assert b"HTTP/1.1 400" in data, data


def test_expect_100_continue(serve_backend_in_thread):
    def body_handler(ctx: HTTPReqCtx) -> None:
        out = b"got:" + ctx.body
        ctx.complete(
            NativeResponse(
                200,
                [
                    (b"content-type", b"text/plain"),
                    (b"content-length", str(len(out)).encode("ascii")),
                ],
            ),
            out,
        )

    def handler(_ctx: HTTPReqCtx):
        return body_handler

    with serve_backend_in_thread(handler) as port:
        with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
            sock.sendall(b"POST /x HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\nExpect: 100-continue\r\n\r\n")
            pre = read_until(sock, b"\r\n\r\n")
            assert b"100 Continue" in pre, pre

            sock.sendall(b"hello")
            final = read_until(sock, b"got:hello")

    assert b"HTTP/1.1 200" in final
    assert b"got:hello" in final


def test_oversize_content_length_returns_413(serve_backend_in_thread):
    called = False

    def handler(ctx: HTTPReqCtx) -> None:
        nonlocal called
        called = True
        ctx.complete(NativeResponse(200, [(b"content-length", b"0")]), b"")

    config = ServerConfig(host="127.0.0.1", port=0, max_body_size=100)
    with serve_backend_in_thread(handler, config) as port:
        r = httpx.post(f"http://127.0.0.1:{port}/", content=b"x" * 1024, timeout=2)

    assert r.status_code == 413
    assert r.text == "Payload Too Large"
    assert called is False


def test_handler_exception_returns_500_and_server_survives(serve_backend_in_thread):
    calls = 0

    def handler(ctx: HTTPReqCtx) -> None:
        nonlocal calls
        calls += 1
        if ctx.request.target == b"/boom":
            raise RuntimeError("handler crashed")
        ctx.complete(NativeResponse(200, [(b"content-length", b"2")]), b"ok")

    with serve_backend_in_thread(handler) as port:
        r1 = httpx.get(f"http://127.0.0.1:{port}/boom", timeout=2)
        r2 = httpx.get(f"http://127.0.0.1:{port}/ok", timeout=2)

    assert r1.status_code == 500
    assert r1.text == "Internal Server Error"
    assert r2.status_code == 200
    assert r2.text == "ok"
    assert calls == 2


def test_streaming_response_chunks(serve_backend_in_thread):
    def handler(ctx: HTTPReqCtx) -> None:
        ctx.start_response(
            NativeResponse(
                status_code=200,
                headers=[],
            )
        )
        ctx.send(b"chunk1")
        ctx.send(b"chunk2")
        ctx.finish_response()

    with serve_backend_in_thread(handler) as port:
        r = httpx.get(f"http://127.0.0.1:{port}/", timeout=2)

    assert r.status_code == 200
    assert r.content == b"chunk1chunk2"


def test_unframed_204_response_has_no_transfer_encoding(serve_backend_in_thread):
    def handler(ctx: HTTPReqCtx) -> None:
        ctx.start_response(NativeResponse(status_code=204, headers=[]))
        ctx.finish_response()

    with serve_backend_in_thread(handler) as port:
        r = httpx.get(f"http://127.0.0.1:{port}/", timeout=2)

    assert r.status_code == 204
    assert "transfer-encoding" not in r.headers
    assert r.content == b""


def test_unframed_304_response_has_no_transfer_encoding(serve_backend_in_thread):
    def handler(ctx: HTTPReqCtx) -> None:
        ctx.start_response(NativeResponse(status_code=304, headers=[]))
        ctx.finish_response()

    with serve_backend_in_thread(handler) as port:
        r = httpx.get(f"http://127.0.0.1:{port}/", timeout=2)

    assert r.status_code == 304
    assert "transfer-encoding" not in r.headers
    assert r.content == b""


def test_unframed_head_response_has_no_body_or_transfer_encoding(serve_backend_in_thread):
    def handler(ctx: HTTPReqCtx) -> None:
        ctx.start_response(NativeResponse(status_code=200, headers=[]))
        ctx.finish_response()

    with serve_backend_in_thread(handler) as port:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
            sock.sendall(b"HEAD / HTTP/1.1\r\nHost: x\r\n\r\n")
            data = read_until(sock, b"\r\n\r\n", deadline=2)
            headers, _, body = data.partition(b"\r\n\r\n")

            sock.settimeout(0.2)
            try:
                extra = sock.recv(16)
            except TimeoutError:
                extra = b""

    assert b"HTTP/1.1 200" in headers
    assert b"transfer-encoding:" not in headers.lower()
    assert body == b""
    assert extra == b""
