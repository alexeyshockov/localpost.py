"""Tests for the HTTP server (localpost.http.server)."""

from __future__ import annotations

import socket

import h11
import httpx
import pytest

from localpost.http import HTTPReqCtx, ServerConfig, start_http_server


# --- Listening-socket lifecycle (no requests) ---------------------------------


class TestStartHttpServer:
    def test_creates_server_with_auto_port(self):
        with start_http_server(ServerConfig(host="127.0.0.1", port=0)) as server:
            assert server.port > 0
            assert server.port != 8000  # auto-assigned, should differ from the default

    def test_server_socket_is_listening(self):
        with start_http_server(ServerConfig(host="127.0.0.1", port=0)) as server:
            with socket.create_connection(("127.0.0.1", server.port), timeout=2):
                pass

    def test_server_socket_closed_after_context(self):
        with start_http_server(ServerConfig(host="127.0.0.1", port=0)) as server:
            port = server.port
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", port), timeout=1)


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
