"""Tests for ``localpost.hosting.rsgi.HostRSGIApp`` — host-as-RSGI for
hosted apps under Granian.

Drives the lifecycle hooks (``__rsgi_init__`` / ``__rsgi__`` /
``__rsgi_del__``) directly with a mocked RSGI scope + proto, bypassing
Granian itself. End-to-end coverage with a real Granian worker lives
in ``tests/openapi/aio_rsgi_integration.py`` (marked ``integration``).

Granian's hooks are sync (``callback_init(loop)``, ``callback_del(loop)``);
:class:`HostRSGIApp` schedules async startup/shutdown on the loop and
gates the first request on a "ready" event. These tests exercise the
gate by yielding control back to the loop after init.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from localpost import hosting
from localpost.hosting.rsgi import HostRSGIApp
from localpost.http import AsyncHTTPReqCtx, Response

# All tests in this file run under asyncio (HostRSGIApp uses asyncio
# primitives — Granian's loop is asyncio-only).
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# --- Mocks --------------------------------------------------------------


class _FakeHeaders:
    def __init__(self, pairs: list[tuple[str, str]] | None = None) -> None:
        self._pairs = pairs or []

    def items(self) -> list[tuple[str, str]]:
        return list(self._pairs)


@dataclass(slots=True, eq=False)
class _FakeScope:
    method: str = "GET"
    path: str = "/"
    query_string: str = ""
    http_version: str = "1.1"
    scheme: str = "http"
    client: str = "127.0.0.1:54321"
    server: str = "127.0.0.1:8000"
    headers: _FakeHeaders = field(default_factory=_FakeHeaders)


@dataclass(slots=True, eq=False)
class _FakeProto:
    body_chunks: list[bytes] = field(default_factory=list)
    response_status: int | None = None
    response_body: bytes | None = None

    async def __call__(self) -> bytes:
        return b"".join(self.body_chunks)

    def __aiter__(self) -> AsyncIterator[bytes]:
        async def gen() -> AsyncIterator[bytes]:
            for c in self.body_chunks:
                yield c

        return gen()

    async def client_disconnect(self) -> None:
        await asyncio.Event().wait()  # never resolves

    def response_empty(self, status: int, headers: Any) -> None:
        self.response_status = status
        self.response_body = b""

    def response_bytes(self, status: int, headers: Any, body: bytes) -> None:
        self.response_status = status
        self.response_body = body


# --- Test services ------------------------------------------------------


def _tracking_service(name: str, log: list[str]) -> hosting.ServiceF:
    """A service that records its lifecycle into ``log``."""

    @hosting.service
    async def svc(sl: hosting.ServiceLifetime) -> None:
        log.append(f"{name}:starting")
        sl.set_started()
        log.append(f"{name}:running")
        await sl.shutting_down.wait()
        log.append(f"{name}:stopping")

    return svc()


# --- Helpers ------------------------------------------------------------


async def _drive_startup(app: HostRSGIApp) -> None:
    """Trigger ``__rsgi_init__`` then yield to the loop until services
    are started — Granian's real flow is async, so we mimic that here."""
    loop = asyncio.get_running_loop()
    app.__rsgi_init__(loop)
    if app._ready is not None:
        await app._ready.wait()


async def _drive_shutdown(app: HostRSGIApp) -> None:
    """Trigger ``__rsgi_del__`` and let the loop drain the lifecycle task."""
    loop = asyncio.get_running_loop()
    app.__rsgi_del__(loop)
    task = app._lifecycle_task
    if task is not None:
        try:
            await asyncio.wait_for(task, timeout=5)
        except asyncio.CancelledError:
            pass


# --- Tests --------------------------------------------------------------


class TestLifecycle:
    async def test_services_started_before_requests(self) -> None:
        log: list[str] = []

        async def handler(ctx: AsyncHTTPReqCtx) -> None:
            log.append("request")
            await ctx.complete(Response(200), b"ok")

        app = HostRSGIApp(
            services=[_tracking_service("svc", log)],
            rsgi_handler=handler,
        )

        await _drive_startup(app)
        # By the time the ready event fires, the service is running.
        assert "svc:starting" in log
        assert "svc:running" in log
        assert "request" not in log

        await app.__rsgi__(_FakeScope(), _FakeProto())
        assert "request" in log

        await _drive_shutdown(app)
        assert "svc:stopping" in log

    async def test_request_dispatch_works(self) -> None:
        async def handler(ctx: AsyncHTTPReqCtx) -> None:
            await ctx.complete(Response(200), b"hello-from-host")

        app = HostRSGIApp(services=[], rsgi_handler=handler)
        await _drive_startup(app)
        try:
            proto = _FakeProto()
            await app.__rsgi__(_FakeScope(), proto)
            assert proto.response_status == 200
            assert proto.response_body == b"hello-from-host"
        finally:
            await _drive_shutdown(app)

    async def test_no_services_no_lifecycle_overhead(self) -> None:
        """When ``services=[]``, init / del are no-ops — a pure dispatch
        path symmetric with :func:`to_rsgi`."""

        async def handler(ctx: AsyncHTTPReqCtx) -> None:
            await ctx.complete(Response(200), b"ok")

        app = HostRSGIApp(services=[], rsgi_handler=handler)
        await _drive_startup(app)
        await _drive_shutdown(app)
        # ``_ready`` stays ``None`` when there are no services — dispatch
        # still works without any wait.
        assert app._ready is None

    async def test_multiple_services_all_run(self) -> None:
        log: list[str] = []

        async def handler(ctx: AsyncHTTPReqCtx) -> None:
            await ctx.complete(Response(200), b"ok")

        app = HostRSGIApp(
            services=[
                _tracking_service("a", log),
                _tracking_service("b", log),
            ],
            rsgi_handler=handler,
        )

        await _drive_startup(app)
        try:
            assert {"a:starting", "b:starting"}.issubset(log)
            assert {"a:running", "b:running"}.issubset(log)
        finally:
            await _drive_shutdown(app)

        assert {"a:stopping", "b:stopping"}.issubset(log)


class TestHandlerResolution:
    async def test_accepts_async_request_handler(self) -> None:
        async def handler(ctx: AsyncHTTPReqCtx) -> None:
            await ctx.complete(Response(200), b"raw")

        app = HostRSGIApp(services=[], rsgi_handler=handler)
        await _drive_startup(app)
        try:
            proto = _FakeProto()
            await app.__rsgi__(_FakeScope(), proto)
            assert proto.response_body == b"raw"
        finally:
            await _drive_shutdown(app)

    async def test_accepts_http_async_app(self) -> None:
        from localpost.openapi import HttpAsyncApp

        oapi_app = HttpAsyncApp(openapi_path=None, docs_path=None)

        @oapi_app.get("/hello")
        async def hello() -> str:
            return "world"

        _ = hello
        app = HostRSGIApp(services=[], rsgi_handler=oapi_app)
        await _drive_startup(app)
        try:
            proto = _FakeProto()
            await app.__rsgi__(_FakeScope(path="/hello"), proto)
            assert proto.response_status == 200
            assert proto.response_body == b"world"
        finally:
            await _drive_shutdown(app)


class TestRequestGate:
    async def test_first_request_waits_for_startup(self) -> None:
        """A request that arrives before startup completes should block
        on the gate until services are ``started``."""
        log: list[str] = []

        @hosting.service
        async def slow_starter(sl: hosting.ServiceLifetime) -> None:
            log.append("starting")
            await asyncio.sleep(0.05)  # simulate a real startup hook
            sl.set_started()
            log.append("started")
            await sl.shutting_down.wait()

        async def handler(ctx: AsyncHTTPReqCtx) -> None:
            log.append("request")
            await ctx.complete(Response(200), b"ok")

        app = HostRSGIApp(services=[slow_starter()], rsgi_handler=handler)
        loop = asyncio.get_running_loop()
        app.__rsgi_init__(loop)
        # Fire the request immediately — must not be served until
        # ``slow_starter`` reports started.
        await app.__rsgi__(_FakeScope(), _FakeProto())
        # Ordering: started must come before the first request.
        started_idx = log.index("started")
        request_idx = log.index("request")
        assert started_idx < request_idx, log

        await _drive_shutdown(app)
