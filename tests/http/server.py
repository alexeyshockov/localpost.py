"""Tests for the HTTP server (localpost.http.server)."""

from __future__ import annotations

import contextlib
import socket
import threading

import httpx
import pytest

from localpost.http import HTTPReqCtx, Response, ServerConfig, start_http_server
from tests.http._helpers import drain_socket

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
            # ConnHandler owns the RequestHandler in the new chain layout
            # (Selector → ConnHandler → RequestHandler → BodyHandler).
            assert server.conn_handler.handler is _noop_handler  # type: ignore[attr-defined]


# --- Request / response basics -----------------------------------------------


class TestBasicRequestResponse:
    def test_simple_200(self, serve_in_thread):
        def handler(ctx: HTTPReqCtx):
            ctx.complete(
                Response(status_code=200, headers=[(b"Content-Type", b"text/plain")]),
                b"OK",
            )

        with serve_in_thread(handler) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/", timeout=5)

        assert resp.status_code == 200
        assert resp.text == "OK"

    def test_404_response(self, serve_in_thread):
        def handler(ctx: HTTPReqCtx):
            ctx.complete(
                Response(status_code=404, headers=[(b"Content-Type", b"text/plain")]),
                b"Not Found",
            )

        with serve_in_thread(handler) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/", timeout=5)

        assert resp.status_code == 404
        assert resp.text == "Not Found"

    def test_empty_body(self, serve_in_thread):
        def handler(ctx: HTTPReqCtx):
            ctx.complete(Response(status_code=204, headers=[]))

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
            ctx.complete(Response(status_code=200, headers=[]), b"")

        with serve_in_thread(handler) as port:
            httpx.post(f"http://127.0.0.1:{port}/api/items?q=1", timeout=5)

        assert captured["method"] == b"POST"
        assert captured["target"] == b"/api/items?q=1"

    def test_handler_sees_headers(self, serve_in_thread):
        captured_headers: dict[bytes, bytes] = {}

        def handler(ctx: HTTPReqCtx):
            captured_headers.update(ctx.request.headers)
            ctx.complete(Response(status_code=200, headers=[]), b"")

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
            ctx.complete(Response(status_code=200, headers=[]), b"ok")

        with serve_in_thread(handler) as port:
            httpx.post(f"http://127.0.0.1:{port}/", content=b"hello world body", timeout=5)

        assert bytes(received_body) == b"hello world body"


class TestChunkedResponse:
    def test_streaming_response(self, serve_in_thread):
        def handler(ctx: HTTPReqCtx):
            ctx.start_response(
                Response(
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
                ctx.complete(Response(status_code=200, headers=[]), b"borrowed")
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
                Response(status_code=200, headers=[(b"Content-Length", b"2")]),
                b"ok",
            )

        with serve_in_thread(handler) as port:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as client:
                client.get("/")
                client.get("/")

        assert call_count == 2

    def test_connection_close_header(self, serve_in_thread):
        """When client sends Connection: close, server should close after one request.

        Also pins the half-close behavior: the next ``recv`` must return a
        clean EOF (b""), not raise ConnectionResetError, since the server
        sends a FIN via shutdown(SHUT_WR) before closing.
        """
        call_count = 0

        def handler(ctx: HTTPReqCtx):
            nonlocal call_count
            call_count += 1
            ctx.complete(
                Response(status_code=200, headers=[(b"content-length", b"2")]),
                b"ok",
            )

        with serve_in_thread(handler) as port:
            with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
                sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                # Read response.
                data = drain_socket(sock, deadline=1.0)
                assert b"HTTP/1.1 200" in data
                # Now expect a clean EOF (FIN), not a reset.
                sock.settimeout(2.0)
                try:
                    eof = sock.recv(64)
                except ConnectionResetError as e:
                    pytest.fail(f"server sent RST instead of FIN: {e}")
                assert eof == b""

        assert call_count == 1


# --- Edge cases ---------------------------------------------------------------


def _ok_handler(ctx: HTTPReqCtx) -> None:
    body = b"ok"
    ctx.complete(
        Response(status_code=200, headers=[(b"content-length", str(len(body)).encode())]),
        body,
    )


class TestProtocolErrors:
    """Server's reaction to malformed input / handler crashes."""

    def test_malformed_request_returns_400_and_keeps_loop_alive(self, serve_in_thread):
        """A garbage request line is caught; the server replies 400 and stays up."""
        with serve_in_thread(_ok_handler) as port:
            with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
                sock.sendall(b"NOT_A_VALID_HTTP_REQUEST\r\n\r\n")
                data = drain_socket(sock, deadline=1.0)
            assert b"HTTP/1.1 400" in data, f"expected 400 in response, got: {data!r}"

            # Sanity: a follow-up valid request is still served.
            resp = httpx.get(f"http://127.0.0.1:{port}/", timeout=2)
            assert resp.status_code == 200

    def test_handler_exception_returns_500(self, serve_in_thread):
        """An unhandled handler exception is caught and returned as 500."""

        def boom(_: HTTPReqCtx) -> None:
            raise RuntimeError("handler crashed")

        with serve_in_thread(boom) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/", timeout=2)

        assert resp.status_code == 500
        assert resp.text == "Internal Server Error"

    def test_handler_exception_after_start_response_closes_conn(self, serve_in_thread):
        """If the handler crashes after start_response, the conn is closed cleanly
        AND the accept loop keeps serving subsequent connections to the same server.
        """
        crash_count = 0
        crash_lock = threading.Lock()

        def boom(ctx: HTTPReqCtx) -> None:
            nonlocal crash_count
            with crash_lock:
                crash_count += 1
            ctx.start_response(
                Response(
                    status_code=200,
                    headers=[(b"transfer-encoding", b"chunked")],
                )
            )
            ctx.send(b"first chunk")
            raise RuntimeError("crashed mid-stream")

        with serve_in_thread(boom) as port:
            # Two separate TCP connections to the SAME server — the second one
            # only succeeds in reaching the handler if the accept loop is
            # still alive after the first crash.
            for _ in range(2):
                with contextlib.suppress(httpx.HTTPError):
                    resp = httpx.get(f"http://127.0.0.1:{port}/", timeout=2)
                    # The response started with 200; httpx may still surface it
                    # despite the truncated body.
                    assert resp.status_code == 200

        assert crash_count == 2, f"expected 2 handler invocations, got {crash_count}"


class TestClientDisconnects:
    def test_client_closes_before_request_complete(self, serve_in_thread):
        """Client opens the conn, sends nothing, closes — server keeps serving others."""
        with serve_in_thread(_ok_handler) as port:
            sock = socket.create_connection(("127.0.0.1", port), timeout=2)
            sock.close()
            # Sanity: the next request still works.
            resp = httpx.get(f"http://127.0.0.1:{port}/", timeout=2)
            assert resp.status_code == 200

    def test_client_half_close_after_partial_body_keeps_loop_alive(self, serve_in_thread):
        """Headers say Content-Length: 100; client sends 5 bytes then half-closes.

        h11 raises RemoteProtocolError on the incomplete body. The server logs,
        closes the conn, and the accept loop survives — verified by a follow-up
        request.
        """
        with serve_in_thread(_ok_handler) as port:
            with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
                sock.sendall(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\nhello")
                sock.shutdown(socket.SHUT_WR)
                drain_socket(sock, deadline=1.0)

            # Server still serves a follow-up valid request.
            resp = httpx.get(f"http://127.0.0.1:{port}/", timeout=2)
            assert resp.status_code == 200


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
                    Response(status_code=200, headers=[(b"content-length", b"2")]),
                    b"ok",
                )

        with serve_in_thread(handler) as port:
            with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
                sock.sendall(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\nExpect: 100-continue\r\n\r\n")
                # Read the 100 Continue intermediate response.
                sock.settimeout(2)
                pre = b""
                while b"\r\n\r\n" not in pre:
                    chunk = sock.recv(4096)
                    assert chunk, "connection closed before 100 Continue"
                    pre += chunk
                assert b"100 Continue" in pre, f"expected 100 Continue, got: {pre!r}"

                sock.sendall(b"hello")
                final = drain_socket(sock, deadline=2.0)

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
            response = Response(status_code=200, headers=headers)
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
                Response(status_code=200, headers=[(b"content-length", str(len(body)).encode())]),
                body,
            )

        with serve_in_thread(handler) as port:
            with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
                sock.sendall(b"GET /a HTTP/1.1\r\nHost: x\r\n\r\nGET /b HTTP/1.1\r\nHost: x\r\n\r\n")
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


# --- Buffer-protocol body ---------------------------------------------------


class TestSendBuffer:
    def test_send_accepts_memoryview(self, serve_in_thread):
        """``ctx.send`` accepts any Buffer (memoryview, bytearray, …)."""

        def handler(ctx: HTTPReqCtx) -> None:
            ctx.start_response(Response(status_code=200, headers=[(b"content-length", b"5")]))
            payload = bytearray(b"hello")
            ctx.send(memoryview(payload)[:5])
            ctx.finish_response()

        with serve_in_thread(handler) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/", timeout=5)

        assert resp.status_code == 200
        assert resp.content == b"hello"


# --- Stale-connection cleanup ------------------------------------------------


class TestStaleCleanup:
    def test_idle_keep_alive_silently_closed_after_timeout(self):
        """An idle keep-alive client sees an EOF (no 408) once keep_alive_timeout elapses."""

        def handler(ctx: HTTPReqCtx) -> None:
            ctx.complete(
                Response(status_code=200, headers=[(b"content-length", b"2")]),
                b"ok",
            )

        cfg = ServerConfig(host="127.0.0.1", port=0, keep_alive_timeout=0.1)
        with start_http_server(cfg, handler) as server:
            stop = threading.Event()
            t = threading.Thread(target=lambda: _run_until(server, stop), daemon=True)
            t.start()
            try:
                with socket.create_connection(("127.0.0.1", server.port), timeout=2) as sock:
                    # First request — server then keeps the connection alive.
                    sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
                    drain_socket(sock, deadline=0.3)
                    # Now sit idle. After keep_alive_timeout (0.1s) + a few iterations,
                    # _cleanup_stale should silently close the conn.
                    sock.settimeout(2.0)
                    data = sock.recv(64)
            finally:
                stop.set()
                t.join(timeout=2)

        assert data == b"", f"expected EOF on idle close, got: {data!r}"

    def test_mid_request_stale_returns_408(self):
        """Client starts sending headers and stops; server emits 408 once stale."""

        def handler(ctx: HTTPReqCtx) -> None:
            ctx.complete(Response(status_code=200, headers=[(b"content-length", b"2")]), b"ok")

        cfg = ServerConfig(host="127.0.0.1", port=0, keep_alive_timeout=0.1, rw_timeout=0.1)
        with start_http_server(cfg, handler) as server:
            stop = threading.Event()
            t = threading.Thread(target=lambda: _run_until(server, stop), daemon=True)
            t.start()
            try:
                with socket.create_connection(("127.0.0.1", server.port), timeout=2) as sock:
                    # Send only part of a request and then sit idle.
                    sock.sendall(b"GET /slow HTTP/1.1\r\nHost: x\r\n")
                    sock.settimeout(2.0)
                    data = drain_socket(sock, deadline=1.5)
            finally:
                stop.set()
                t.join(timeout=2)

        assert b"HTTP/1.1 408" in data, f"expected 408 in response, got: {data!r}"


# --- Body limit ---------------------------------------------------------------


class TestBodyLimit:
    def test_oversized_content_length_returns_413(self):
        """Server short-circuits on Content-Length > max_body_size."""
        captured: dict = {"called": False}

        def handler(ctx: HTTPReqCtx) -> None:
            captured["called"] = True
            ctx.complete(Response(status_code=200, headers=[(b"content-length", b"2")]), b"ok")

        cfg = ServerConfig(host="127.0.0.1", port=0, max_body_size=10)
        with start_http_server(cfg, handler) as server:
            stop = threading.Event()
            t = threading.Thread(
                target=lambda: _run_until(server, stop),
                daemon=True,
            )
            t.start()
            try:
                resp = httpx.post(
                    f"http://127.0.0.1:{server.port}/",
                    content=b"x" * 1000,
                    timeout=2,
                )
            finally:
                stop.set()
                t.join(timeout=2)

        assert resp.status_code == 413
        assert resp.text == "Payload Too Large"
        assert captured["called"] is False  # handler never invoked

    def test_oversized_streaming_body_returns_413(self):
        """A handler that reads the body sees BodyTooLarge once the cap is crossed."""
        captured: dict = {"raised": False}

        def handler(ctx: HTTPReqCtx) -> None:
            try:
                while ctx.receive():
                    pass
            except Exception as e:
                captured["raised"] = type(e).__name__
                raise

        cfg = ServerConfig(host="127.0.0.1", port=0, max_body_size=8)
        with start_http_server(cfg, handler) as server:
            stop = threading.Event()
            t = threading.Thread(target=lambda: _run_until(server, stop), daemon=True)
            t.start()
            try:
                # No Content-Length: send chunked to bypass the up-front check.
                with socket.create_connection(("127.0.0.1", server.port), timeout=3) as sock:
                    sock.sendall(
                        b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
                        b"10\r\n" + (b"x" * 16) + b"\r\n0\r\n\r\n"
                    )
                    data = drain_socket(sock, deadline=2.0)
            finally:
                stop.set()
                t.join(timeout=2)

        assert b"HTTP/1.1 413" in data, f"expected 413, got: {data!r}"
        assert captured["raised"] == "BodyTooLarge"


def _run_until(server, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            server.run(timeout=0.05)
        except OSError:
            return


# --- Graceful shutdown -------------------------------------------------------


class TestGracefulShutdown:
    def test_shutting_down_flag_set_on_exit(self):
        """``Server.shutting_down`` flips to True on context-manager exit."""
        captured: dict = {}
        with start_http_server(ServerConfig(host="127.0.0.1", port=0), _noop_handler) as server:
            captured["server"] = server
            assert server.shutting_down is False
        assert captured["server"].shutting_down is True

    def test_idle_keep_alive_connection_closed_on_exit(self, serve_in_thread):
        """A keep-alive socket sees an EOF (recv → b"") once the server exits."""

        def handler(ctx: HTTPReqCtx) -> None:
            ctx.complete(
                Response(status_code=200, headers=[(b"content-length", b"2")]),
                b"ok",
            )

        with serve_in_thread(handler) as port:
            sock = socket.create_connection(("127.0.0.1", port), timeout=2)
            try:
                # Send a request and read its response — connection is kept alive.
                sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
                drain_socket(sock, deadline=0.5)
            finally:
                # Pull socket reference outside the with block so we can recv after server exit.
                pass

        # Outside `serve_in_thread`: server has exited and closed the keep-alive conn.
        sock.settimeout(2.0)
        try:
            data = sock.recv(64)
        except (ConnectionResetError, OSError):
            data = b""
        finally:
            sock.close()
        assert data == b"", f"expected EOF after shutdown, got: {data!r}"
