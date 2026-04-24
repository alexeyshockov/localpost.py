"""Tests for localpost.http.flask — the native Flask adapter."""

from __future__ import annotations

import threading

import httpx
import pytest
from flask import Flask, Response, stream_with_context
from flask import request as flask_request

from localpost import threadtools
from localpost.http import ServerConfig, start_http_server
from localpost.http.flask import flask_handler


@pytest.fixture(autouse=True)
def _disable_cancellation_check(monkeypatch):
    monkeypatch.setattr(threadtools, "check_cancelled", lambda: None)


@pytest.fixture
def server_config():
    return ServerConfig(host="127.0.0.1", port=0)


def _run_iterations(server, handler, n=20):
    for _ in range(n):
        try:
            server.run(handler)
        except (OSError, ValueError, AttributeError):
            return


def _serve(server_config, app: Flask, request_fn):
    """Run the server in a thread, call request_fn(port), return whatever it returns."""
    with start_http_server(server_config) as server:
        t = threading.Thread(target=_run_iterations, args=(server, flask_handler(app)))
        t.start()
        try:
            return request_fn(server.port)
        finally:
            t.join(timeout=5)


class TestFlaskHandler:
    def test_simple_200(self, server_config):
        app = Flask(__name__)

        @app.route("/")
        def index():
            return "hello flask"

        assert index

        resp = _serve(server_config, app, lambda port: httpx.get(f"http://127.0.0.1:{port}/"))
        assert resp.status_code == 200
        assert resp.text == "hello flask"

    def test_path_parameters(self, server_config):
        app = Flask(__name__)

        @app.route("/hello/<name>")
        def hello(name: str):
            return f"hi {name}"

        assert hello

        resp = _serve(server_config, app, lambda port: httpx.get(f"http://127.0.0.1:{port}/hello/alice"))
        assert resp.status_code == 200
        assert resp.text == "hi alice"

    def test_post_body(self, server_config):
        app = Flask(__name__)
        captured: dict = {}

        @app.route("/echo", methods=["POST"])
        def echo():
            captured["body"] = flask_request.get_data()
            return Response(captured["body"], mimetype="application/octet-stream")

        assert echo

        resp = _serve(
            server_config,
            app,
            lambda port: httpx.post(f"http://127.0.0.1:{port}/echo", content=b"payload"),
        )
        assert captured.get("body") == b"payload", f"status={resp.status_code}, body={resp.content!r}"
        assert resp.status_code == 200
        assert resp.content == b"payload"

    def test_flask_404(self, server_config):
        app = Flask(__name__)

        resp = _serve(server_config, app, lambda port: httpx.get(f"http://127.0.0.1:{port}/missing"))
        assert resp.status_code == 404

    def test_view_exception_returns_500(self, server_config):
        app = Flask(__name__)

        @app.route("/boom")
        def boom():
            raise RuntimeError("bang")

        assert boom

        resp = _serve(server_config, app, lambda port: httpx.get(f"http://127.0.0.1:{port}/boom"))
        assert resp.status_code == 500

    def test_streaming_without_stream_with_context(self, server_config):
        """Key behavior test: generator uses flask.request without @stream_with_context."""
        app = Flask(__name__)

        @app.route("/stream")
        def stream():
            def generate():
                # Would normally raise "Working outside of request context"
                # under standard WSGI without @stream_with_context.
                yield "ua="
                yield flask_request.headers.get("User-Agent", "?") + "\n"

            return Response(generate(), mimetype="text/plain")

        assert stream

        resp = _serve(
            server_config,
            app,
            lambda port: httpx.get(
                f"http://127.0.0.1:{port}/stream",
                headers={"User-Agent": "test-client"},
            ),
        )
        assert resp.status_code == 200
        assert resp.text == "ua=test-client\n"

    def test_streaming_with_stream_with_context_still_works(self, server_config):
        """Backwards compat: @stream_with_context is a no-op but must not break."""
        app = Flask(__name__)

        @app.route("/stream-ctx")
        def stream_ctx():
            def generate():
                yield "ua="
                yield flask_request.headers.get("User-Agent", "?") + "\n"

            return Response(stream_with_context(generate()), mimetype="text/plain")

        assert stream_ctx

        resp = _serve(
            server_config,
            app,
            lambda port: httpx.get(
                f"http://127.0.0.1:{port}/stream-ctx",
                headers={"User-Agent": "test-client"},
            ),
        )
        assert resp.status_code == 200
        assert resp.text == "ua=test-client\n"

    def test_teardown_runs_after_body_sent(self, server_config):
        """teardown_request fires AFTER response iteration completes, not before.

        This is the opposite of standard WSGI Flask behavior.
        """
        app = Flask(__name__)
        events: list[str] = []
        events_lock = threading.Lock()

        def log(ev: str) -> None:
            with events_lock:
                events.append(ev)

        @app.teardown_request
        def _teardown(exc):
            log("teardown")

        @app.route("/ordering")
        def ordering():
            log("view-start")

            def generate():
                log("chunk-1")
                yield b"one"
                log("chunk-2")
                yield b"two"
                log("chunk-end")

            return Response(generate(), mimetype="text/plain")

        assert ordering

        resp = _serve(server_config, app, lambda port: httpx.get(f"http://127.0.0.1:{port}/ordering"))
        assert resp.status_code == 200
        assert resp.content == b"onetwo"

        # Expected order: view-start → (chunks iterate) → teardown
        with events_lock:
            captured = list(events)
        assert captured == ["view-start", "chunk-1", "chunk-2", "chunk-end", "teardown"]

    def test_response_headers_are_forwarded(self, server_config):
        app = Flask(__name__)

        @app.route("/with-header")
        def with_header():
            return Response("body", mimetype="text/plain", headers={"X-Custom": "yes"})

        assert with_header

        resp = _serve(server_config, app, lambda port: httpx.get(f"http://127.0.0.1:{port}/with-header"))
        assert resp.status_code == 200
        assert resp.headers.get("x-custom") == "yes"
        assert resp.headers.get("content-type", "").startswith("text/plain")
