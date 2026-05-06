"""End-to-end HTTP tests against ``HttpAsyncApp`` deployed under uvicorn.

The unit suite (``aio_app.py``) drives the ASGI callable directly via
``httpx.ASGITransport`` — fast, no socket. These tests run the full stack
under a real uvicorn instance to catch what in-process drivers can't:

- Header round-trip through h11 + uvicorn.
- ASGI lifespan negotiation.
- Real chunked SSE delivery (per-event flushing on the wire).
- Peer-disconnect mid-stream.

uvicorn runs in a background thread per-test (its own asyncio loop); the
test reaches it over loopback. Mirrors ``tests/openapi/conftest.py``'s
sync ``serve_app`` shape so the fixtures read alike.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass

import httpx
import pytest
import uvicorn

from localpost.openapi import (
    BadRequest,
    Created,
    HttpAsyncApp,
    NotFound,
    spec,
)

# Keep these tests off the unit run; they cost ~1-2s each.
pytestmark = pytest.mark.integration


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _UvicornThread:
    """Run uvicorn in a daemon thread, surface its port to the test.

    uvicorn drives its own asyncio loop; we just hand it the ASGI app
    (``HttpAsyncApp.asgi()``) and a free port. ``__enter__`` blocks until
    the server is accepting connections, ``__exit__`` triggers a graceful
    shutdown.
    """

    def __init__(self, app: HttpAsyncApp, port: int) -> None:
        self._asgi = app.asgi()
        self._port = port
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None

    def __enter__(self) -> int:
        config = uvicorn.Config(
            app=self._asgi,
            host="127.0.0.1",
            port=self._port,
            log_level="warning",
            access_log=False,
            loop="asyncio",
            lifespan="on",
        )
        server = uvicorn.Server(config)
        self._server = server

        def run() -> None:
            try:
                asyncio.run(server.serve())
            except BaseException as e:  # noqa: BLE001
                self._error = e

        self._thread = threading.Thread(target=run, daemon=True, name=f"uvicorn-{self._port}")
        self._thread.start()
        # Wait for "started" — uvicorn flips ``server.started`` once the
        # socket is listening. Bound to ~5s so a stuck startup fails fast.
        deadline = time.monotonic() + 5.0
        while not server.started and time.monotonic() < deadline:
            if self._error is not None:
                raise self._error
            time.sleep(0.01)
        if not server.started:
            raise RuntimeError("uvicorn failed to start within 5s")
        return self._port

    def __exit__(self, *_exc: object) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._error is not None:
            raise self._error


ServeAsyncApp = Callable[[HttpAsyncApp], _UvicornThread]


@pytest.fixture
def serve_async_app() -> Iterator[ServeAsyncApp]:
    active: list[_UvicornThread] = []

    def make(app: HttpAsyncApp) -> _UvicornThread:
        return _UvicornThread(app, _free_port())

    yield make

    # If a test forgot to ``__exit__``, drain anything still in-flight.
    for t in active:
        if t._server is not None:
            t._server.should_exit = True
        if t._thread is not None and t._thread.is_alive():
            t._thread.join(timeout=5)


# --- Sample app ----------------------------------------------------------


@dataclass
class Book:
    id: str
    title: str


@pytest.fixture
def library_app() -> HttpAsyncApp:
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

    @app.get("/clock")
    async def clock() -> AsyncIterator[str]:
        # Three quick events for SSE round-trip / peer-disconnect tests.
        # Cap the loop so a hung client doesn't keep the worker alive.
        for i in range(3):
            yield f"tick-{i}"
            await asyncio.sleep(0.05)

    _ = (hello, get_book, create_book, clock)
    return app


# --- Tests ---------------------------------------------------------------


class TestRoundTrip:
    def test_hello_world(self, serve_async_app: ServeAsyncApp, library_app: HttpAsyncApp) -> None:
        with serve_async_app(library_app) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/hello/world", timeout=5)
        assert resp.status_code == 200
        assert resp.text == "Hello, world!"
        assert resp.headers["content-type"].startswith("text/plain")

    def test_op_result_404_branch(self, serve_async_app: ServeAsyncApp, library_app: HttpAsyncApp) -> None:
        with serve_async_app(library_app) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/books/missing", timeout=5)
        assert resp.status_code == 404
        assert resp.text == "Book not found: missing"

    def test_post_json_round_trip(self, serve_async_app: ServeAsyncApp, library_app: HttpAsyncApp) -> None:
        with serve_async_app(library_app) as port:
            resp = httpx.post(
                f"http://127.0.0.1:{port}/books",
                json={"id": "7", "title": "Dune"},
                timeout=5,
            )
        assert resp.status_code == 201
        assert resp.headers["Location"] == "/books/7"
        assert resp.json() == {"id": "7", "title": "Dune"}


class TestBuiltInRoutes:
    def test_openapi_json(self, serve_async_app: ServeAsyncApp, library_app: HttpAsyncApp) -> None:
        with serve_async_app(library_app) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/openapi.json", timeout=5)
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/json"
        doc = resp.json()
        assert doc["openapi"].startswith("3.2")
        assert "/hello/{name}" in doc["paths"]
        assert "Book" in doc["components"]["schemas"]

    def test_swagger_ui(self, serve_async_app: ServeAsyncApp, library_app: HttpAsyncApp) -> None:
        with serve_async_app(library_app) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/docs", timeout=5)
        assert resp.status_code == 200
        assert b"Swagger UI" in resp.content


class TestSSE:
    def test_chunked_event_delivery(self, serve_async_app: ServeAsyncApp, library_app: HttpAsyncApp) -> None:
        """Exercise the wire: each event must arrive as a separate
        ``\\n\\n``-terminated block; ``content-length`` must be absent
        (chunked transfer)."""
        with serve_async_app(library_app) as port:
            with httpx.stream("GET", f"http://127.0.0.1:{port}/clock", timeout=5) as resp:
                assert resp.status_code == 200
                assert resp.headers["content-type"].startswith("text/event-stream")
                assert "content-length" not in resp.headers
                wire = b"".join(resp.iter_bytes())
        assert wire.count(b"\n\n") == 3
        assert b"data: tick-0\n\n" in wire
        assert b"data: tick-2\n\n" in wire

    def test_peer_disconnect_mid_stream(self, serve_async_app: ServeAsyncApp, library_app: HttpAsyncApp) -> None:
        """Close the response stream after one event; the server-side
        generator should be cancelled (no leaked tasks, server stays
        responsive for the next request)."""
        with serve_async_app(library_app) as port:
            base = f"http://127.0.0.1:{port}"
            # Read just one event then disconnect.
            with httpx.stream("GET", f"{base}/clock", timeout=5) as resp:
                assert resp.status_code == 200
                iterator: Iterator[bytes] = resp.iter_bytes()
                first = next(iterator)
                assert b"data: tick-0" in first
                # Closing the response forces the underlying connection
                # closed → uvicorn raises http.disconnect to the app.
                resp.close()
            # Sanity: the server is still alive for a follow-up request.
            resp = httpx.get(f"{base}/hello/world", timeout=5)
        assert resp.status_code == 200


class TestPayloadLimits:
    def test_413_on_oversized_body(self, serve_async_app: ServeAsyncApp) -> None:
        app = HttpAsyncApp(max_body_size=8)

        @app.post("/b")
        async def create(book: Book) -> Created[Book]:
            return Created(book)

        with serve_async_app(app) as port:
            resp = httpx.post(
                f"http://127.0.0.1:{port}/b",
                content=b'{"id":"1","title":"way-too-long"}',
                headers={"content-type": "application/json"},
                timeout=5,
            )
        assert resp.status_code == 413
