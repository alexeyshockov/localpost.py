"""Shared HTTP behavior contract across the h11 and httptools backends."""

from __future__ import annotations

import socket

import httpx

from localpost.http import HTTPReqCtx, Response, ServerConfig, read_body
from tests.http._helpers import drain_socket, read_http_response, read_until


def _ok(body: bytes = b"hello"):
    def handler(ctx: HTTPReqCtx) -> None:
        ctx.complete(
            Response(
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


def test_buffered_post_body_handler(serve_backend_in_thread, http_backend):
    if http_backend == "httptools":
        # ``read_body`` from on-selector handlers re-enters httptools'
        # ``parser.feed_data`` callback chain — only safe via a worker
        # pool. The pooled equivalent lives in ``tests/http/service.py``;
        # here we just exercise the h11 path.
        import pytest as _pytest  # noqa: PLC0415

        _pytest.skip("httptools requires worker-pool composition for body reads")

    received: list[bytes] = []

    def handler(ctx: HTTPReqCtx) -> None:
        body = read_body(ctx)
        received.append(body)
        out = b"echo:" + body
        ctx.complete(
            Response(
                status_code=200,
                headers=[
                    (b"content-type", b"text/plain"),
                    (b"content-length", str(len(out)).encode("ascii")),
                ],
            ),
            out,
        )

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
            Response(
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
        ctx.complete(Response(200, [(b"content-length", b"0")]), b"")

    with serve_backend_in_thread(handler) as port:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
            sock.sendall(b"NOT-AN-HTTP-REQUEST\r\n\r\n")
            data = drain_socket(sock, deadline=1.0)
    assert b"HTTP/1.1 400" in data, data


def test_expect_100_continue(serve_backend_in_thread, http_backend):
    if http_backend == "httptools":
        import pytest as _pytest  # noqa: PLC0415

        _pytest.skip("httptools requires worker-pool composition for body reads")

    def handler(ctx: HTTPReqCtx) -> None:
        # ``read_body`` drives the body recv loop, which sends 100 Continue
        # when the parser sees the client is awaiting it.
        body = read_body(ctx)
        out = b"got:" + body
        ctx.complete(
            Response(
                200,
                [
                    (b"content-type", b"text/plain"),
                    (b"content-length", str(len(out)).encode("ascii")),
                ],
            ),
            out,
        )

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
        ctx.complete(Response(200, [(b"content-length", b"0")]), b"")

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
        ctx.complete(Response(200, [(b"content-length", b"2")]), b"ok")

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
        ctx.stream(
            Response(
                status_code=200,
                headers=[],
            ),
            iter([b"chunk1", b"chunk2"]),
        )

    with serve_backend_in_thread(handler) as port:
        r = httpx.get(f"http://127.0.0.1:{port}/", timeout=2)

    assert r.status_code == 200
    assert r.content == b"chunk1chunk2"


def test_unframed_204_response_has_no_transfer_encoding(serve_backend_in_thread):
    def handler(ctx: HTTPReqCtx) -> None:
        ctx.complete(Response(status_code=204, headers=[]))

    with serve_backend_in_thread(handler) as port:
        r = httpx.get(f"http://127.0.0.1:{port}/", timeout=2)

    assert r.status_code == 204
    assert "transfer-encoding" not in r.headers
    assert r.content == b""


def test_unframed_304_response_has_no_transfer_encoding(serve_backend_in_thread):
    def handler(ctx: HTTPReqCtx) -> None:
        ctx.complete(Response(status_code=304, headers=[]))

    with serve_backend_in_thread(handler) as port:
        r = httpx.get(f"http://127.0.0.1:{port}/", timeout=2)

    assert r.status_code == 304
    assert "transfer-encoding" not in r.headers
    assert r.content == b""


def test_unframed_head_response_has_no_body_or_transfer_encoding(serve_backend_in_thread):
    def handler(ctx: HTTPReqCtx) -> None:
        ctx.complete(Response(status_code=200, headers=[]))

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


def test_user_supplied_chunked_te_frames_chunks(serve_backend_in_thread):
    """Regression: a handler that explicitly sets ``Transfer-Encoding: chunked``
    must still get its ``send(...)`` chunks framed on the wire. Earlier the
    httptools backend only set its internal ``_chunked`` flag inside the
    auto-frame branch, so user-supplied TE bypassed framing and the wire
    was malformed.
    """
    import socket  # noqa: PLC0415

    def handler(ctx: HTTPReqCtx) -> None:
        ctx.stream(
            Response(
                status_code=200,
                headers=[
                    (b"content-type", b"text/plain"),
                    (b"transfer-encoding", b"chunked"),
                ],
            ),
            iter([b"chunk1", b"chunk2"]),
        )

    with serve_backend_in_thread(handler) as port, socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        s.settimeout(5)
        buf = b""
        while True:
            chunk = s.recv(8192)
            if not chunk:
                break
            buf += chunk

    head, _, body = buf.partition(b"\r\n\r\n")
    assert b"transfer-encoding: chunked" in head.lower()
    # Each ``send`` produces one chunk on the wire, framed as
    # ``<hex-size>\r\n<bytes>\r\n``. Verify both are present, then the
    # zero-length terminator.
    assert b"6\r\nchunk1\r\n" in body
    assert b"6\r\nchunk2\r\n" in body
    assert body.endswith(b"0\r\n\r\n")
