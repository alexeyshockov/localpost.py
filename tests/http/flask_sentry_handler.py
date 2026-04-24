"""Tests for localpost.http.flask_sentry — focus on the streaming-in-same-transaction fix."""

from __future__ import annotations

import threading

import httpx
import pytest
import sentry_sdk
from flask import Flask
from flask import Response as FlaskResponse
from flask import request as flask_request
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport

from localpost import threadtools
from localpost.http import ServerConfig, start_http_server
from localpost.http.flask_sentry import sentry_flask_handler


class CapturingTransport(Transport):
    def __init__(self) -> None:
        super().__init__({})
        self.envelopes: list[Envelope] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        self.envelopes.append(envelope)

    def flush(self, timeout: float, callback=None) -> None:  # type: ignore[override]
        pass


def _transactions(transport: CapturingTransport) -> list[dict]:
    out: list[dict] = []
    for env in transport.envelopes:
        for item in env.items:
            if item.headers.get("type") == "transaction":
                payload = item.payload.json
                if payload is not None:
                    out.append(payload)
    return out


@pytest.fixture(autouse=True)
def _disable_cancellation_check(monkeypatch):
    monkeypatch.setattr(threadtools, "check_cancelled", lambda: None)


@pytest.fixture
def server_config():
    return ServerConfig(host="127.0.0.1", port=0)


@pytest.fixture
def sentry_transport():
    transport = CapturingTransport()
    sentry_sdk.init(
        dsn="https://public@example.com/1",
        transport=transport,
        traces_sample_rate=1.0,
        default_integrations=False,
        auto_enabling_integrations=False,
    )
    yield transport
    sentry_sdk.flush(timeout=2.0)


def _run_iterations(server, handler, n=20):
    for _ in range(n):
        try:
            server.run(handler)
        except (OSError, ValueError, AttributeError):
            return


def _serve(server_config, handler, request_fn):
    with start_http_server(server_config) as server:
        t = threading.Thread(target=_run_iterations, args=(server, handler))
        t.start()
        try:
            return request_fn(server.port)
        finally:
            t.join(timeout=5)


class TestSentryFlaskHandler:
    def test_transaction_named_after_url_rule(self, server_config, sentry_transport):
        app = Flask(__name__)

        @app.route("/hello/<name>")
        def hello(name: str):
            return f"hi {name}"

        assert hello

        resp = _serve(
            server_config,
            sentry_flask_handler(app),
            lambda port: httpx.get(f"http://127.0.0.1:{port}/hello/alice"),
        )
        assert resp.status_code == 200
        sentry_sdk.flush(timeout=2.0)

        txs = _transactions(sentry_transport)
        assert len(txs) == 1
        tx = txs[0]
        assert tx["transaction"] == "GET /hello/<name>"
        assert tx["transaction_info"]["source"] == "route"
        assert tx["contexts"]["trace"]["op"] == "http.server"

    def test_unmatched_url_keeps_url_source(self, server_config, sentry_transport):
        app = Flask(__name__)

        resp = _serve(
            server_config,
            sentry_flask_handler(app),
            lambda port: httpx.get(f"http://127.0.0.1:{port}/no-such-route"),
        )
        assert resp.status_code == 404
        sentry_sdk.flush(timeout=2.0)

        txs = _transactions(sentry_transport)
        assert len(txs) == 1
        assert txs[0]["transaction"] == "GET /no-such-route"
        assert txs[0]["transaction_info"]["source"] == "url"

    def test_streaming_span_lands_on_same_transaction(self, server_config, sentry_transport):
        """The reason this adapter exists: spans emitted inside a streaming
        generator must land on the same transaction as the request, not on a
        new (or no) transaction. Stock Sentry FlaskIntegration ends the
        transaction before the WSGI server iterates the body.
        """
        app = Flask(__name__)

        @app.route("/stream/<name>")
        def stream(name: str):
            def generate():
                # Span emitted DURING body iteration. Must land on the request transaction.
                with sentry_sdk.start_span(op="custom.streaming-chunk", name="emit-body"):
                    yield f"hi {name}\n".encode()
                # Touch flask.request to prove context is alive.
                yield f"ua={flask_request.headers.get('User-Agent', '?')}\n".encode()

            return FlaskResponse(generate(), mimetype="text/plain")

        assert stream

        resp = _serve(
            server_config,
            sentry_flask_handler(app),
            lambda port: httpx.get(
                f"http://127.0.0.1:{port}/stream/alice",
                headers={"User-Agent": "test"},
            ),
        )
        assert resp.status_code == 200
        assert b"hi alice" in resp.content
        assert b"ua=test" in resp.content
        sentry_sdk.flush(timeout=2.0)

        txs = _transactions(sentry_transport)
        assert len(txs) == 1
        tx = txs[0]
        assert tx["transaction"] == "GET /stream/<name>"

        # The streaming span must be in this transaction's spans.
        span_ops = [s.get("op") for s in tx.get("spans", [])]
        assert "custom.streaming-chunk" in span_ops, (
            f"streaming span did not land on the transaction; spans={tx.get('spans')}"
        )

    def test_view_exception_records_500(self, server_config, sentry_transport):
        app = Flask(__name__)

        @app.route("/boom")
        def boom():
            raise RuntimeError("bang")

        assert boom

        resp = _serve(
            server_config,
            sentry_flask_handler(app),
            lambda port: httpx.get(f"http://127.0.0.1:{port}/boom"),
        )
        assert resp.status_code == 500
        sentry_sdk.flush(timeout=2.0)

        txs = _transactions(sentry_transport)
        assert len(txs) == 1
        trace = txs[0]["contexts"]["trace"]
        status = trace.get("data", {}).get("http.response.status_code") or txs[0]["tags"].get("http.status_code")
        assert status in (500, "500")
