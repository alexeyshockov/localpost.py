"""Tests for the HTTP server (localpost.http.server)."""

import socket
import threading

import h11
import pytest

from localpost import threadtools
from localpost.http.config import ServerConfig
from localpost.http.server import HTTPReqCtx, start_http_server


@pytest.fixture(autouse=True)
def _disable_cancellation_check(monkeypatch):
    """Server loop calls check_cancelled(), which requires AnyIO context. Disable it for sync tests."""
    monkeypatch.setattr(threadtools, "check_cancelled", lambda: None)


@pytest.fixture
def server_config():
    return ServerConfig(host="127.0.0.1", port=0)  # port=0 → auto-assign


def _send_raw(port: int, data: bytes) -> bytes:
    """Send raw bytes to the server and return the full response."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(data)
        chunks = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)


def _h11_request(port: int, method: str = "GET", target: str = "/", headers=None, body: bytes | None = None):
    """Send a request via h11 client and return (response, body)."""
    conn = h11.Connection(h11.CLIENT)
    hdrs = list(headers or [])
    hdrs.append(("Host", "localhost"))
    if body is not None:
        hdrs.append(("Content-Length", str(len(body))))
    req = h11.Request(method=method, target=target, headers=hdrs)

    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(conn.send(req))
        if body is not None:
            sock.sendall(conn.send(h11.Data(data=body)))
        sock.sendall(conn.send(h11.EndOfMessage()))

        response = None
        body_chunks = []

        while True:
            event = conn.next_event()
            if event is h11.NEED_DATA:
                data = sock.recv(4096)
                conn.receive_data(data)
                continue
            if isinstance(event, h11.Response):
                response = event
            elif isinstance(event, h11.Data):
                body_chunks.append(event.data)
            elif isinstance(event, h11.EndOfMessage):
                break
            elif isinstance(event, h11.ConnectionClosed | h11.NEED_DATA.__class__):
                break

        return response, b"".join(body_chunks)


def _run_server_iterations(server, handler, n=10):
    """Run the server loop for a fixed number of iterations."""
    for _ in range(n):
        try:
            server.run(handler)
        except OSError:
            return  # Server socket closed (context manager exited)


# --- Tests ---


class TestStartHttpServer:
    def test_creates_server_with_auto_port(self, server_config):
        with start_http_server(server_config) as server:
            assert server.port > 0
            assert server.port != 8000  # auto-assigned, should differ from the default

    def test_server_socket_is_listening(self, server_config):
        with start_http_server(server_config) as server:
            # Should be able to connect
            with socket.create_connection(("127.0.0.1", server.port), timeout=2):
                pass

    def test_server_socket_closed_after_context(self, server_config):
        with start_http_server(server_config) as server:
            port = server.port
        # Socket should be closed, connection should fail
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", port), timeout=1)


class TestBasicRequestResponse:
    def test_simple_200(self, server_config):
        def handler(ctx: HTTPReqCtx):
            ctx.complete(
                h11.Response(status_code=200, headers=[(b"Content-Type", b"text/plain")]),
                b"OK",
            )

        with start_http_server(server_config) as server:
            t = threading.Thread(target=_run_server_iterations, args=(server, handler))
            t.start()

            resp, body = _h11_request(server.port)

            t.join(timeout=5)

        assert resp is not None
        assert resp.status_code == 200
        assert body == b"OK"

    def test_404_response(self, server_config):
        def handler(ctx: HTTPReqCtx):
            ctx.complete(
                h11.Response(status_code=404, headers=[(b"Content-Type", b"text/plain")]),
                b"Not Found",
            )

        with start_http_server(server_config) as server:
            t = threading.Thread(target=_run_server_iterations, args=(server, handler))
            t.start()

            resp, body = _h11_request(server.port)

            t.join(timeout=5)

        assert resp.status_code == 404
        assert body == b"Not Found"

    def test_empty_body(self, server_config):
        def handler(ctx: HTTPReqCtx):
            ctx.complete(h11.Response(status_code=204, headers=[]))

        with start_http_server(server_config) as server:
            t = threading.Thread(target=_run_server_iterations, args=(server, handler))
            t.start()

            resp, body = _h11_request(server.port)

            t.join(timeout=5)

        assert resp.status_code == 204
        assert body == b""


class TestRequestRouting:
    def test_handler_sees_method_and_target(self, server_config):
        captured = {}

        def handler(ctx: HTTPReqCtx):
            captured["method"] = ctx.request.method
            captured["target"] = ctx.request.target
            ctx.complete(h11.Response(status_code=200, headers=[]), b"")

        with start_http_server(server_config) as server:
            t = threading.Thread(target=_run_server_iterations, args=(server, handler))
            t.start()
            _h11_request(server.port, method="POST", target="/api/items?q=1")
            t.join(timeout=5)

        assert captured["method"] == b"POST"
        assert captured["target"] == b"/api/items?q=1"

    def test_handler_sees_headers(self, server_config):
        captured_headers = {}

        def handler(ctx: HTTPReqCtx):
            for name, value in ctx.request.headers:
                captured_headers[name] = value
            ctx.complete(h11.Response(status_code=200, headers=[]), b"")

        with start_http_server(server_config) as server:
            t = threading.Thread(target=_run_server_iterations, args=(server, handler))
            t.start()
            _h11_request(server.port, headers=[("X-Custom", "hello")])
            t.join(timeout=5)

        assert captured_headers[b"x-custom"] == b"hello"


class TestRequestBody:
    def test_receive_post_body(self, server_config):
        received_body = bytearray()

        def handler(ctx: HTTPReqCtx):
            with ctx.borrow():
                while True:
                    chunk = ctx.receive()
                    if not chunk:
                        break
                    received_body.extend(chunk)
            ctx.complete(h11.Response(status_code=200, headers=[]), b"ok")

        with start_http_server(server_config) as server:
            t = threading.Thread(target=_run_server_iterations, args=(server, handler, 20))
            t.start()

            payload = b"hello world body"
            _h11_request(server.port, method="POST", body=payload)

            t.join(timeout=5)

        assert bytes(received_body) == b"hello world body"


class TestChunkedResponse:
    def test_streaming_response(self, server_config):
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

        with start_http_server(server_config) as server:
            t = threading.Thread(target=_run_server_iterations, args=(server, handler))
            t.start()

            resp, body = _h11_request(server.port)

            t.join(timeout=5)

        assert resp.status_code == 200
        assert body == b"chunk1chunk2"


class TestBorrow:
    def test_borrow_and_return(self, server_config):
        borrow_states = []

        def handler(ctx: HTTPReqCtx):
            borrow_states.append(ctx.borrowed)  # False — still tracked
            with ctx.borrow():
                borrow_states.append(ctx.borrowed)  # True — untracked
                ctx.complete(h11.Response(status_code=200, headers=[]), b"borrowed")
            borrow_states.append(ctx.borrowed)  # False — re-tracked after finish_response

        with start_http_server(server_config) as server:
            t = threading.Thread(target=_run_server_iterations, args=(server, handler, 15))
            t.start()
            _h11_request(server.port)
            t.join(timeout=5)

        assert borrow_states == [False, True, False]


class TestKeepAlive:
    def test_multiple_requests_on_same_connection(self, server_config):
        call_count = 0

        def handler(ctx: HTTPReqCtx):
            nonlocal call_count
            call_count += 1
            ctx.complete(
                h11.Response(status_code=200, headers=[(b"Content-Length", b"2")]),
                b"ok",
            )

        with start_http_server(server_config) as server:
            t = threading.Thread(target=_run_server_iterations, args=(server, handler, 30))
            t.start()

            # Use a single h11 client connection for two requests
            client = h11.Connection(h11.CLIENT)
            with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
                for _ in range(2):
                    req = h11.Request(
                        method="GET", target="/", headers=[("Host", "localhost")]
                    )
                    sock.sendall(client.send(req))
                    sock.sendall(client.send(h11.EndOfMessage()))

                    while True:
                        event = client.next_event()
                        if event is h11.NEED_DATA:
                            data = sock.recv(4096)
                            client.receive_data(data)
                            continue
                        if isinstance(event, h11.EndOfMessage):
                            break
                    client.start_next_cycle()

            t.join(timeout=5)

        assert call_count == 2

    def test_connection_close_header(self, server_config):
        """When client sends Connection: close, server should close after one request."""
        call_count = 0

        def handler(ctx: HTTPReqCtx):
            nonlocal call_count
            call_count += 1
            ctx.complete(
                h11.Response(status_code=200, headers=[(b"Content-Length", b"2")]),
                b"ok",
            )

        with start_http_server(server_config) as server:
            t = threading.Thread(target=_run_server_iterations, args=(server, handler))
            t.start()

            _h11_request(server.port, headers=[("Connection", "close")])

            t.join(timeout=5)

        assert call_count == 1
