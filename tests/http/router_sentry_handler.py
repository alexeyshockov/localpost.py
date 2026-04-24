"""Tests for localpost.http.router_sentry."""

from __future__ import annotations

import threading

import httpx
import pytest
import sentry_sdk
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport

from localpost import threadtools
from localpost.http import RequestCtx, Response, Routes, ServerConfig, start_http_server
from localpost.http.router_sentry import sentry_router_handler


class CapturingTransport(Transport):
    """Sentry transport that just records envelopes (no network)."""

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
        # Disable default integrations that might pollute envelopes.
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


def _build_router():
    routes = Routes()

    @routes.get("/books/{id}")
    def get_book(ctx: RequestCtx) -> Response:
        return Response(200, {"content-type": "text/plain"}, [f"book={ctx.path_args['id']}".encode()])

    @routes.post("/books")
    def create_book(_: RequestCtx) -> Response:
        return Response(201, {}, [])

    assert get_book is not None
    assert create_book is not None
    return routes.build()


class TestSentryRouterHandler:
    def test_matched_route_uses_template_name(self, server_config, sentry_transport):
        router = _build_router()
        handler = sentry_router_handler(router)

        resp = _serve(
            server_config,
            handler,
            lambda port: httpx.get(f"http://127.0.0.1:{port}/books/42"),
        )
        assert resp.status_code == 200
        sentry_sdk.flush(timeout=2.0)

        txs = _transactions(sentry_transport)
        assert len(txs) == 1
        tx = txs[0]
        assert tx["transaction"] == "GET /books/{id}"
        assert tx["transaction_info"]["source"] == "route"
        # Op lives under contexts.trace.op
        assert tx["contexts"]["trace"]["op"] == "http.server"

    def test_unmatched_uses_url_source(self, server_config, sentry_transport):
        router = _build_router()
        handler = sentry_router_handler(router)

        resp = _serve(
            server_config,
            handler,
            lambda port: httpx.get(f"http://127.0.0.1:{port}/does-not-exist"),
        )
        assert resp.status_code == 404
        sentry_sdk.flush(timeout=2.0)

        txs = _transactions(sentry_transport)
        assert len(txs) == 1
        tx = txs[0]
        assert tx["transaction"] == "GET /does-not-exist"
        assert tx["transaction_info"]["source"] == "url"

    def test_status_code_is_recorded(self, server_config, sentry_transport):
        router = _build_router()
        handler = sentry_router_handler(router)

        _serve(
            server_config,
            handler,
            lambda port: httpx.post(f"http://127.0.0.1:{port}/books"),
        )
        sentry_sdk.flush(timeout=2.0)

        txs = _transactions(sentry_transport)
        assert len(txs) == 1
        tx = txs[0]
        # Sentry stores http status under contexts.trace.data["http.response.status_code"]
        # (older SDKs put it on tags). Check both.
        trace_data = tx["contexts"]["trace"].get("data", {})
        status = trace_data.get("http.response.status_code") or tx.get("tags", {}).get("http.status_code")
        assert status in (201, "201")

    def test_method_tag_recorded(self, server_config, sentry_transport):
        router = _build_router()
        handler = sentry_router_handler(router)

        _serve(
            server_config,
            handler,
            lambda port: httpx.get(f"http://127.0.0.1:{port}/books/1"),
        )
        sentry_sdk.flush(timeout=2.0)

        txs = _transactions(sentry_transport)
        assert len(txs) == 1
        assert txs[0]["tags"].get("http.method") == "GET"
