"""Tests for the HTTP server (localpost.http.server)."""

from __future__ import annotations

import contextlib
import socket
import threading

import h11
import httpx
import pytest

from localpost.http import HTTPReqCtx, ServerConfig, start_http_server


def _drain(sock: socket.socket, deadline: float = 2.0) -> bytes:
    """Read until the peer closes or the deadline elapses."""
    sock.settimeout(deadline)
    out = bytearray()
    while True:
        try:
            chunk = sock.recv(4096)
        except (TimeoutError, socket.timeout):
            break
        if not chunk:
            break
        out.extend(chunk)
    return bytes(out)


# --- Listening-socket lifecycle (no requests) ---------------------------------


def _noop_handler(ctx: HTTPReqCtx) -> None:
    pass


class TestStartHttpServer:
    def test_creates_server_with_auto_port(self):
        with start_http_server(ServerConfig(host="127.0.0.1", port=0), _noop_handler) as server:
            assert server.port > 0
            assert server.port != 8000  # auto-assigned, should differ from the default

    def test_server_socket_is_listening(self):
        with start_http_server(ServerConfig(host="127.0.0.1", port=0), _noop_handler) as server:
            with socket.create_connection(("127.0.0.1", server.port), timeout=2):
                pass

    def test_server_socket_closed_after_context(self):
        with start_http_server(ServerConfig(host="127.0.0.1", port=0), _noop_handler) as server:
            port = server.port
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", port), timeout=1)

    def test_handler_stored_on_server(self):
        with start_http_server(ServerConfig(host="127.0.0.1", port=0), _noop_handler) as server:
            assert server.handler is _noop_handler


# --- Request / response basics -----------------------------------------------


class TestBasicRequestResponse:
    def test_simple_200(self, serve_in_thread):
        def handler(ctx: HTTPReqCtx):
            ctx.complete(
                h11.Response(status_code=200, headers=[(b"Content-Type", b"text/plain")]),
                b"OK",
            )

        with serve_in_thread(handler) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/", timeout=5)

        assert resp.status_code == 200
        assert resp.text == "OK"

    def test_404_response(self, serve_in_thread):
        def handler(ctx: HTTPReqCtx):
            ctx.complete(
                h11.Response(status_code=404, headers=[(b"Content-Type", b"text/plain")]),
                b"Not Found",
            )

        with serve_in_thread(handler) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/", timeout=5)

        assert resp.status_code == 404
        assert resp.text == "Not Found"

    def test_empty_body(self, serve_in_thread):
        def handler(ctx: HTTPReqCtx):
            ctx.complete(h11.Response(status_code=204, headers=[]))

        with serve_in_thread(handler) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/", timeout=5)

        assert resp.status_code == 204
        assert resp.content == b""


class TestRequestRouting:
    def test_handler_sees_method_and_target(self, serve_in_thread):
        captured = {}

        def handler(ctx: HTTPReqCtx):
            captured["method"] = ctx.request.method
            captured["target"] = ctx.request.target
            ctx.complete(h11.Response(status_code=200, headers=[]), b"")

        with serve_in_thread(handler) as port:
            httpx.post(f"http://127.0.0.1:{port}/api/items?q=1", timeout=5)

        assert captured["method"] == b"POST"
        assert captured["target"] == b"/api/items?q=1"

    def test_handler_sees_headers(self, serve_in_thread):
        captured_headers: dict[bytes, bytes] = {}

        def handler(ctx: HTTPReqCtx):
            for name, value in ctx.request.headers:
                captured_headers[name] = value
            ctx.complete(h11.Response(status_code=200, headers=[]), b"")

        with serve_in_thread(handler) as port:
            httpx.get(f"http://127.0.0.1:{port}/", headers={"X-Custom": "hello"}, timeout=5)

        assert captured_headers[b"x-custom"] == b"hello"


class TestRequestBody:
    def test_receive_post_body(self, serve_in_thread):
        received_body = bytearray()

        def handler(ctx: HTTPReqCtx):
            while True:
                chunk = ctx.receive()
                if not chunk:
                    break
                received_body.extend(chunk)
            ctx.complete(h11.Response(status_code=200, headers=[]), b"ok")

        with serve_in_thread(handler) as port:
            httpx.post(f"http://127.0.0.1:{port}/", content=b"hello world body", timeout=5)

        assert bytes(received_body) == b"hello world body"


class TestChunkedResponse:
    def test_streaming_response(self, serve_in_thread):
        def handler(ctx: HTTPReqCtx):
            ctx.start_response(
                h11.Response(
                    status_code=200,
                    headers=[(b"Transfer-Encoding", b"chunked")],
                )
            )
            ctx.send(b"chunk1")
            ctx.send(b"chunk2")
            ctx.finish_response()

        with serve_in_thread(handler) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/", timeout=5)

        assert resp.status_code == 200
        assert resp.content == b"chunk1chunk2"


class TestBorrow:
    def test_borrow_and_return(self, serve_in_thread):
        borrow_states = []

        def handler(ctx: HTTPReqCtx):
            borrow_states.append(ctx.borrowed)  # False — still tracked
            with ctx.borrow():
                borrow_states.append(ctx.borrowed)  # True — untracked
                ctx.complete(h11.Response(status_code=200, headers=[]), b"borrowed")
            borrow_states.append(ctx.borrowed)  # False — re-tracked after finish_response

        with serve_in_thread(handler) as port:
            httpx.get(f"http://127.0.0.1:{port}/", timeout=5)

        assert borrow_states == [False, True, False]


class TestKeepAlive:
    def test_multiple_requests_on_same_connection(self, serve_in_thread):
        call_count = 0

        def handler(ctx: HTTPReqCtx):
            nonlocal call_count
            call_count += 1
            ctx.complete(
                h11.Response(status_code=200, headers=[(b"Content-Length", b"2")]),
                b"ok",
            )

        with serve_in_thread(handler) as port:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as client:
                client.get("/")
                client.get("/")

        assert call_count == 2

    def test_connection_close_header(self, serve_in_thread):
        """When client sends Connection: close, server should close after one request."""
        call_count = 0

        def handler(ctx: HTTPReqCtx):
            nonlocal call_count
            call_count += 1
            ctx.complete(
                h11.Response(status_code=200, headers=[(b"Content-Length", b"2")]),
                b"ok",
            )

        with serve_in_thread(handler) as port:
            httpx.get(f"http://127.0.0.1:{port}/", headers={"Connection": "close"}, timeout=5)

        assert call_count == 1


# --- Edge cases ---------------------------------------------------------------


def _ok_handler(ctx: HTTPReqCtx) -> None:
    body = b"ok"
    ctx.complete(
        h11.Response(status_code=200, headers=[(b"content-length", str(len(body)).encode())]),
        body,
    )


class TestProtocolErrors:
    """Server's reaction to malformed input / handler crashes.

    These pin *current* behavior — the server has no try/except around
    handler invocation or h11 parsing (see TODOs in server.py:__call__),
    so both paths kill the loop. When the server is hardened the assertions
    here will need to flip from ``expect_loop_error=True`` to a 4xx/5xx check.
    """

    def test_malformed_request_kills_loop_today(self, serve_in_thread):
        """A garbage request line propagates h11.RemoteProtocolError out of the loop."""
        cm = serve_in_thread(_ok_handler)
        cm.expect_loop_error = True

        with cm as port:
            with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
                sock.sendall(b"NOT_A_VALID_HTTP_REQUEST\r\n\r\n")
                _drain(sock, deadline=1.0)

        assert isinstance(cm.loop_error, h11.RemoteProtocolError)

    def test_handler_exception_returns_500(self, serve_in_thread):
        """An unhandled handler exception is caught and returned as 500."""

        def boom(_: HTTPReqCtx) -> None:
            raise RuntimeError("handler crashed")

        with serve_in_thread(boom) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/", timeout=2)

        assert resp.status_code == 500
        assert resp.text == "Internal Server Error"

    def test_handler_exception_after_start_response_closes_conn(self, serve_in_thread):
        """If the handler crashes after start_response, the conn is closed cleanly."""

        def boom(ctx: HTTPReqCtx) -> None:
            ctx.start_response(
                h11.Response(
                    status_code=200,
                    headers=[(b"transfer-encoding", b"chunked")],
                )
            )
            ctx.send(b"first chunk")
            raise RuntimeError("crashed mid-stream")

        with serve_in_thread(boom) as port:
            with contextlib.suppress(httpx.HTTPError):
                resp = httpx.get(f"http://127.0.0.1:{port}/", timeout=2)
                # Response started with 200, but body is truncated when conn closes.
                assert resp.status_code == 200

            # Server still serves a follow-up request — the loop survived.
            with serve_in_thread(_ok_handler) as second_port:
                resp2 = httpx.get(f"http://127.0.0.1:{second_port}/", timeout=2)
                assert resp2.status_code == 200


class TestClientDisconnects:
    def test_client_closes_before_request_complete(self, serve_in_thread):
        """Client opens the conn, sends nothing, closes — server keeps serving others."""
        with serve_in_thread(_ok_handler) as port:
            sock = socket.create_connection(("127.0.0.1", port), timeout=2)
            sock.close()
            # Sanity: the next request still works.
            resp = httpx.get(f"http://127.0.0.1:{port}/", timeout=2)
            assert resp.status_code == 200

    def test_client_half_close_after_partial_body_kills_loop_today(self, serve_in_thread):
        """Headers say Content-Length: 100, client sends 5 bytes then half-closes.

        h11 raises on incomplete body and the loop has no try/except around
        the parser, so the server thread dies. Pin as expected behavior until
        ``HTTPConn.__call__`` gains protocol-error handling (TODO at server.py).
        """
        cm = serve_in_thread(_ok_handler)
        cm.expect_loop_error = True

        with cm as port:
            with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
                sock.sendall(
                    b"POST / HTTP/1.1\r\n"
                    b"Host: x\r\n"
                    b"Content-Length: 100\r\n"
                    b"\r\n"
                    b"hello"
                )
                sock.shutdown(socket.SHUT_WR)
                _drain(sock, deadline=1.0)

        assert isinstance(cm.loop_error, h11.RemoteProtocolError)


class TestExpect100Continue:
    def test_handler_reads_body_triggers_100_continue(self, serve_in_thread):
        """When the handler calls receive() with Expect:100, the server emits 100 first.

        The handler must ``borrow()`` so the socket is in blocking mode while
        it waits for the body — otherwise the non-blocking recv races the
        client's post-100 send.
        """
        captured = bytearray()

        def handler(ctx: HTTPReqCtx) -> None:
            with ctx.borrow():
                while True:
                    chunk = ctx.receive()
                    if not chunk:
                        break
                    captured.extend(chunk)
                ctx.complete(
                    h11.Response(status_code=200, headers=[(b"content-length", b"2")]),
                    b"ok",
                )

        with serve_in_thread(handler) as port:
            with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
                sock.sendall(
                    b"POST / HTTP/1.1\r\n"
                    b"Host: x\r\n"
                    b"Content-Length: 5\r\n"
                    b"Expect: 100-continue\r\n"
                    b"\r\n"
                )
                # Read the 100 Continue intermediate response.
                sock.settimeout(2)
                pre = b""
                while b"\r\n\r\n" not in pre:
                    chunk = sock.recv(4096)
                    assert chunk, "connection closed before 100 Continue"
                    pre += chunk
                assert b"100 Continue" in pre, f"expected 100 Continue, got: {pre!r}"

                sock.sendall(b"hello")
                final = _drain(sock, deadline=2.0)

        assert b"HTTP/1.1 200" in final
        assert b"\r\n\r\nok" in final
        assert bytes(captured) == b"hello"


class TestHeadAndPipelining:
    def test_head_request(self, serve_in_thread):
        """HEAD response carries headers but no body bytes.

        Note: a HEAD-aware handler must skip ``send(body)`` — h11 raises
        ``Too much data for declared Content-Length`` if the server tries to
        write a body for HEAD. This test pins that contract.
        """
        body = b"this would be the body"

        def handler(ctx: HTTPReqCtx) -> None:
            headers = [
                (b"content-type", b"text/plain"),
                (b"content-length", str(len(body)).encode()),
            ]
            response = h11.Response(status_code=200, headers=headers)
            if ctx.request.method == b"HEAD":
                ctx.complete(response, None)
            else:
                ctx.complete(response, body)

        with serve_in_thread(handler) as port:
            resp = httpx.head(f"http://127.0.0.1:{port}/", timeout=5)

        assert resp.status_code == 200
        assert resp.headers["content-length"] == str(len(body))
        assert resp.content == b""

    def test_pipelined_requests_on_one_connection(self, serve_in_thread):
        """Two requests written in one TCP send are both served in order."""
        served: list[bytes] = []
        served_lock = threading.Lock()

        def handler(ctx: HTTPReqCtx) -> None:
            with served_lock:
                served.append(ctx.request.target)
            body = b"resp-for-" + ctx.request.target
            ctx.complete(
                h11.Response(status_code=200, headers=[(b"content-length", str(len(body)).encode())]),
                body,
            )

        with serve_in_thread(handler) as port:
            with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
                sock.sendall(
                    b"GET /a HTTP/1.1\r\nHost: x\r\n\r\n"
                    b"GET /b HTTP/1.1\r\nHost: x\r\n\r\n"
                )
                # Read until both bodies have arrived.
                sock.settimeout(2)
                data = b""
                while b"resp-for-/a" not in data or b"resp-for-/b" not in data:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    data += chunk

        assert served == [b"/a", b"/b"]
        assert b"resp-for-/a" in data
        assert b"resp-for-/b" in data
