"""End-to-end RSGI tests under a real Granian server.

The unit suites (``tests/http/rsgi.py`` and ``tests/hosting/rsgi.py``)
drive the bridge with a mocked proto. These tests run the full stack
under :class:`granian.server.embed.Server` to catch what in-process
mocks miss — header round-trip through the real RSGI implementation,
chunked SSE delivery, and Mode B (host-as-RSGI) with a background
service running alongside the HTTP handler.

Each test spins Granian on a free loopback port for the duration of
the test, then shuts it down cleanly.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest
from granian.constants import Interfaces
from granian.server.embed import Server

from localpost import hosting
from localpost.openapi import (
    BadRequest,
    Created,
    HttpAsyncApp,
    NotFound,
    spec,
)

# These tests cost ~1-2s each; keep them off the unit run.
pytestmark = pytest.mark.integration


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _GranianThread:
    """Run :class:`granian.server.embed.Server` on its own asyncio loop
    in a daemon thread — same shape as the uvicorn fixture in
    ``aio_integration.py``."""

    def __init__(self, target: object, port: int) -> None:
        self._target = target
        self._port = port
        self._loop: asyncio.AbstractEventLoop | None = None
        self._serve_task: asyncio.Task[None] | None = None
        self._server: Server | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def __enter__(self) -> int:
        def run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop

            self._server = Server(
                target=self._target,
                address="127.0.0.1",
                port=self._port,
                interface=Interfaces.RSGI,
                log_enabled=False,
                log_access=False,
            )

            async def serve_then_signal() -> None:
                # Wait briefly for the server to bind, then mark ready.
                # Granian's embed Server doesn't expose a "started" event,
                # so we poll the port instead.
                pass

            try:
                self._serve_task = loop.create_task(self._server.serve())
                self._ready.set()
                loop.run_until_complete(self._serve_task)
            except Exception:  # noqa: BLE001
                self._ready.set()
            finally:
                loop.close()

        self._thread = threading.Thread(target=run, daemon=True, name=f"granian-{self._port}")
        self._thread.start()
        self._ready.wait(timeout=5)
        # Wait for the port to actually be accepting connections.
        deadline = asyncio.get_event_loop_policy().new_event_loop().time() + 5.0
        while True:
            try:
                with socket.create_connection(("127.0.0.1", self._port), timeout=0.1):
                    break
            except OSError:
                if asyncio.get_event_loop_policy().new_event_loop().time() > deadline:
                    raise RuntimeError("Granian failed to start within 5s") from None
                threading.Event().wait(0.05)
        return self._port

    def __exit__(self, *_exc: object) -> None:
        loop = self._loop
        srv = self._server
        if loop is not None and srv is not None:
            with contextlib.suppress(Exception):
                fut = asyncio.run_coroutine_threadsafe(srv.shutdown(), loop)
                fut.result(timeout=5)
            with contextlib.suppress(Exception):
                loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5)


# --- Sample app ----------------------------------------------------------


@dataclass
class Book:
    id: str
    title: str


def _library_app() -> HttpAsyncApp:
    app = HttpAsyncApp(info=spec.Info(title="Library API", version="1.0.0"))
    library: dict[str, Book] = {"42": Book(id="42", title="HHGTTG")}

    @app.get("/hello/{name}")
    async def hello(name: str) -> str | BadRequest[str]:
        if name.lower() == "donald":
            return BadRequest("Sorry, you are not welcome here")
        return f"Hello, {name}!"

    @app.get("/books/{book_id}")
    async def get_book(book_id: str) -> Book | NotFound[str]:
        book = library.get(book_id)
        if book is None:
            return NotFound(f"Book not found: {book_id}")
        return book

    @app.post("/books")
    async def create_book(book: Book) -> Created[Book]:
        library[book.id] = book
        return Created(book, headers={"Location": f"/books/{book.id}"})

    _ = (hello, get_book, create_book)
    return app


# --- Tests ---------------------------------------------------------------


class TestModeARoundTrip:
    """``HttpAsyncApp.as_rsgi()`` deployed under Granian."""

    def test_hello_world(self) -> None:
        app = _library_app()
        with _GranianThread(app.as_rsgi(), _free_port()) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/hello/world", timeout=5)
        assert resp.status_code == 200
        assert resp.text == "Hello, world!"

    def test_op_result_404(self) -> None:
        app = _library_app()
        with _GranianThread(app.as_rsgi(), _free_port()) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/books/missing", timeout=5)
        assert resp.status_code == 404
        assert resp.text == "Book not found: missing"

    def test_post_json(self) -> None:
        app = _library_app()
        with _GranianThread(app.as_rsgi(), _free_port()) as port:
            resp = httpx.post(
                f"http://127.0.0.1:{port}/books",
                json={"id": "7", "title": "Dune"},
                timeout=5,
            )
        assert resp.status_code == 201
        assert resp.headers["Location"] == "/books/7"
        assert resp.json() == {"id": "7", "title": "Dune"}

    def test_openapi_endpoint(self) -> None:
        app = _library_app()
        with _GranianThread(app.as_rsgi(), _free_port()) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/openapi.json", timeout=5)
        assert resp.status_code == 200
        doc = resp.json()
        assert doc["openapi"].startswith("3.2")
        assert "/hello/{name}" in doc["paths"]


class TestModeBHostedApp:
    """``HostRSGIApp`` — background service runs in the same Granian
    worker as the HTTP handler."""

    def test_background_service_runs_alongside_handler(self) -> None:
        # A heartbeat service that ticks while the worker is up.
        ticks: list[float] = []
        ticks_lock = threading.Lock()

        @hosting.service
        async def heartbeat(sl: hosting.ServiceLifetime) -> None:
            sl.set_started()
            with contextlib.suppress(Exception):
                while not sl.shutting_down.is_set():
                    with ticks_lock:
                        ticks.append(0.0)
                    # Don't actually sleep on real time — just yield enough
                    # for a few ticks during the test.
                    await asyncio.sleep(0.05)

        app = _library_app()
        rsgi_app = hosting.HostRSGIApp(
            services=[heartbeat()],
            rsgi_handler=app,
        )

        with _GranianThread(rsgi_app, _free_port()) as port:
            # Hit the HTTP side a couple of times.
            r1 = httpx.get(f"http://127.0.0.1:{port}/hello/world", timeout=5)
            r2 = httpx.get(f"http://127.0.0.1:{port}/books/42", timeout=5)

        assert r1.status_code == 200
        assert r2.status_code == 200
        # The background service ticked at least once during the test.
        with ticks_lock:
            assert len(ticks) >= 1, "heartbeat service did not tick"


class TestSSE:
    """Real chunked SSE delivery through Granian's response_stream path."""

    def test_event_stream_round_trip(self) -> None:
        app = HttpAsyncApp(openapi_path=None, docs_path=None)

        @app.get("/clock")
        async def clock() -> AsyncIterator[str]:
            for i in range(3):
                yield f"tick-{i}"
                await asyncio.sleep(0.05)

        _ = clock
        with _GranianThread(app.as_rsgi(), _free_port()) as port:
            with httpx.stream("GET", f"http://127.0.0.1:{port}/clock", timeout=5) as resp:
                assert resp.status_code == 200
                assert resp.headers["content-type"].startswith("text/event-stream")
                wire = b"".join(resp.iter_bytes())
        assert wire.count(b"\n\n") == 3
        assert b"data: tick-0\n\n" in wire
        assert b"data: tick-2\n\n" in wire
