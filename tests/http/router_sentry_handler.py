"""Tests for localpost.http.router_sentry."""

from __future__ import annotations

import httpx
import pytest
import sentry_sdk

from localpost.http import BodyHandler, HTTPReqCtx, Response, Routes, route_match
from localpost.http.router_sentry import sentry_router_handler

from ._sentry_helpers import CapturingTransport, init_sentry, transactions


@pytest.fixture
def sentry_transport():
    transport = CapturingTransport()
    init_sentry(transport)
    yield transport
    sentry_sdk.flush(timeout=2.0)


def _build_router():
    routes = Routes()

    @routes.get("/books/{id}")
    def get_book(ctx: HTTPReqCtx) -> BodyHandler | None:
        body = f"book={route_match(ctx).path_args['id']}".encode()
        ctx.complete(
            Response(
                status_code=200,
                headers=[(b"content-type", b"text/plain"), (b"content-length", str(len(body)).encode("ascii"))],
            ),
            body,
        )
        return None

    @routes.post("/books")
    def create_book(ctx: HTTPReqCtx) -> BodyHandler | None:
        ctx.complete(Response(status_code=201, headers=[(b"content-length", b"0")]), b"")
        return None

    assert get_book is not None
    assert create_book is not None
    return routes.build()


class TestSentryRouterHandler:
    def test_matched_route_uses_template_name(self, serve_in_thread, sentry_transport):
        router = _build_router()

        with serve_in_thread(sentry_router_handler(router)) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/books/42", timeout=5)
        assert resp.status_code == 200
        sentry_sdk.flush(timeout=2.0)

        txs = transactions(sentry_transport)
        assert len(txs) == 1
        tx = txs[0]
        assert tx["transaction"] == "GET /books/{id}"
        assert tx["transaction_info"]["source"] == "route"
        # Op lives under contexts.trace.op
        assert tx["contexts"]["trace"]["op"] == "http.server"

    def test_unmatched_uses_url_source(self, serve_in_thread, sentry_transport):
        router = _build_router()

        with serve_in_thread(sentry_router_handler(router)) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/does-not-exist", timeout=5)
        assert resp.status_code == 404
        sentry_sdk.flush(timeout=2.0)

        txs = transactions(sentry_transport)
        assert len(txs) == 1
        tx = txs[0]
        assert tx["transaction"] == "GET /does-not-exist"
        assert tx["transaction_info"]["source"] == "url"

    def test_status_code_is_recorded(self, serve_in_thread, sentry_transport):
        router = _build_router()

        with serve_in_thread(sentry_router_handler(router)) as port:
            httpx.post(f"http://127.0.0.1:{port}/books", timeout=5)
        sentry_sdk.flush(timeout=2.0)

        txs = transactions(sentry_transport)
        assert len(txs) == 1
        tx = txs[0]
        # Sentry stores http status under contexts.trace.data["http.response.status_code"]
        # (older SDKs put it on tags). Check both.
        trace_data = tx["contexts"]["trace"].get("data", {})
        status = trace_data.get("http.response.status_code") or tx.get("tags", {}).get("http.status_code")
        assert status in (201, "201")

    def test_method_tag_recorded(self, serve_in_thread, sentry_transport):
        router = _build_router()

        with serve_in_thread(sentry_router_handler(router)) as port:
            httpx.get(f"http://127.0.0.1:{port}/books/1", timeout=5)
        sentry_sdk.flush(timeout=2.0)

        txs = transactions(sentry_transport)
        assert len(txs) == 1
        assert txs[0]["tags"].get("http.method") == "GET"
