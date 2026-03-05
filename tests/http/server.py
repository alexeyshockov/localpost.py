"""Tests for the HTTP server (localpost.http.server)."""

import socket
import threading

import h11
import httpx
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


def _run_server_iterations(server, handler, n=10):
    """Run the server loop for a fixed number of iterations."""
    for _ in range(n):
        try:
            server.run(handler)
        except (OSError, ValueError):
            return  # Server socket/selector closed (context manager exited)


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

            resp = httpx.get(f"http://127.0.0.1:{server.port}/")

            t.join(timeout=5)

        assert resp.status_code == 200
        assert resp.text == "OK"

    def test_404_response(self, server_config):
        def handler(ctx: HTTPReqCtx):
            ctx.complete(
                h11.Response(status_code=404, headers=[(b"Content-Type", b"text/plain")]),
                b"Not Found",
            )

        with start_http_server(server_config) as server:
            t = threading.Thread(target=_run_server_iterations, args=(server, handler))
            t.start()

            resp = httpx.get(f"http://127.0.0.1:{server.port}/")

            t.join(timeout=5)

        assert resp.status_code == 404
        assert resp.text == "Not Found"

    def test_empty_body(self, server_config):
        def handler(ctx: HTTPReqCtx):
            ctx.complete(h11.Response(status_code=204, headers=[]))

        with start_http_server(server_config) as server:
            t = threading.Thread(target=_run_server_iterations, args=(server, handler))
            t.start()

            resp = httpx.get(f"http://127.0.0.1:{server.port}/")

            t.join(timeout=5)

        assert resp.status_code == 204
        assert resp.content == b""


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
            httpx.post(f"http://127.0.0.1:{server.port}/api/items?q=1")
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
            httpx.get(f"http://127.0.0.1:{server.port}/", headers={"X-Custom": "hello"})
            t.join(timeout=5)

        assert captured_headers[b"x-custom"] == b"hello"


class TestRequestBody:
    def test_receive_post_body(self, server_config):
        received_body = bytearray()

        def handler(ctx: HTTPReqCtx):
            while True:
                chunk = ctx.receive()
                if not chunk:
                    break
                received_body.extend(chunk)
            ctx.complete(h11.Response(status_code=200, headers=[]), b"ok")

        with start_http_server(server_config) as server:
            t = threading.Thread(target=_run_server_iterations, args=(server, handler, 20))
            t.start()

            httpx.post(f"http://127.0.0.1:{server.port}/", content=b"hello world body")

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

            resp = httpx.get(f"http://127.0.0.1:{server.port}/")

            t.join(timeout=5)

        assert resp.status_code == 200
        assert resp.content == b"chunk1chunk2"


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
            httpx.get(f"http://127.0.0.1:{server.port}/")
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

            # httpx.Client reuses the TCP connection (keep-alive by default)
            with httpx.Client(base_url=f"http://127.0.0.1:{server.port}") as client:
                client.get("/")
                client.get("/")

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

            httpx.get(f"http://127.0.0.1:{server.port}/", headers={"Connection": "close"})

            t.join(timeout=5)

        assert call_count == 1
