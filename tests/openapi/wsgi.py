"""Tests for ``HttpApp.as_wsgi()`` — OpenAPI app under WSGI deployment.

Drives the WSGI app directly (no live server). Covers the operation
pipeline, OpenAPI doc serving, and SSE streaming through ``ctx.stream``.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from localpost.openapi import Event, HttpApp, NotFound


@dataclass
class Pet:
    name: str
    age: int


@dataclass
class Book:
    id: str


def _drive(app: Any, environ: dict[str, Any]) -> tuple[str, list[tuple[str, str]], bytes, Iterable[bytes]]:
    """Returns (status, headers, joined_body_bytes, body_iter).

    Some tests need to inspect the iterator's chunk shape (lazy streaming),
    so we return both the joined bytes and the underlying iter.
    """
    captured: dict[str, Any] = {}

    def start_response(status: str, headers: list[tuple[str, str]], exc_info: Any = None) -> Any:
        captured["status"] = status
        captured["headers"] = headers
        return lambda _: None

    body_iter: Iterable[bytes] = app(environ, start_response)
    body = b"".join(body_iter)
    close = getattr(body_iter, "close", None)
    if close is not None:
        close()
    return captured["status"], captured["headers"], body, body_iter


def _get(path: str, query: str = "", headers: dict[str, str] | None = None) -> dict[str, Any]:
    environ: dict[str, Any] = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "SERVER_NAME": "localhost",
        "SERVER_PORT": "8000",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(b""),
        "wsgi.errors": io.StringIO(),
        "wsgi.multithread": True,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
        "CONTENT_LENGTH": "",
        "CONTENT_TYPE": "",
        "REMOTE_ADDR": "127.0.0.1",
        "REMOTE_PORT": "5555",
    }
    if headers:
        for k, v in headers.items():
            environ["HTTP_" + k.upper().replace("-", "_")] = v
    return environ


def _post_json(path: str, payload: bytes) -> dict[str, Any]:
    environ = _get(path)
    environ["REQUEST_METHOD"] = "POST"
    environ["wsgi.input"] = io.BytesIO(payload)
    environ["CONTENT_LENGTH"] = str(len(payload))
    environ["CONTENT_TYPE"] = "application/json"
    return environ


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


class TestHttpAppAsWsgiBasic:
    def test_simple_get_string_return(self):
        app = HttpApp()

        @app.get("/hello/{name}")
        def hello(name: str) -> str:
            return f"Hello, {name}!"

        wsgi_app = app.as_wsgi()
        status, headers, body, _ = _drive(wsgi_app, _get("/hello/world"))
        assert status.startswith("200 ")
        assert ("content-type", "text/plain; charset=utf-8") in headers
        assert body == b"Hello, world!"

    def test_post_json_body(self):
        app = HttpApp()

        @app.post("/pets")
        def create(pet: Pet) -> Pet:
            return pet

        wsgi_app = app.as_wsgi()
        status, _headers, body, _ = _drive(wsgi_app, _post_json("/pets", b'{"name":"Rex","age":3}'))
        assert status.startswith("200 ")
        assert json.loads(body) == {"name": "Rex", "age": 3}

    def test_query_param(self):
        app = HttpApp()

        @app.get("/search")
        def search(q: str, limit: int = 10) -> dict[str, Any]:
            return {"q": q, "limit": limit}

        wsgi_app = app.as_wsgi()
        status, _headers, body, _ = _drive(wsgi_app, _get("/search", "q=hello&limit=5"))
        assert status.startswith("200 ")
        assert json.loads(body) == {"q": "hello", "limit": 5}

    def test_404_via_null_return(self):
        app = HttpApp()

        @app.get("/books/{book_id}")
        def get_book(book_id: str) -> Book | NotFound[str]:
            if book_id == "42":
                return Book(id=book_id)
            return NotFound(f"no such book: {book_id}")

        wsgi_app = app.as_wsgi()
        status, _headers, body, _ = _drive(wsgi_app, _get("/books/99"))
        assert status.startswith("404 ")
        assert b"no such book: 99" in body

    def test_router_404_for_unknown_path(self):
        app = HttpApp()

        @app.get("/x")
        def x() -> str:
            return "x"

        wsgi_app = app.as_wsgi()
        status, _headers, body, _ = _drive(wsgi_app, _get("/y"))
        assert status.startswith("404 ")
        assert body == b"Not Found"


class TestHttpAppAsWsgiOpenAPIDoc:
    def test_serves_openapi_json(self):
        app = HttpApp()

        @app.get("/hello")
        def hello() -> str:
            return "hi"

        wsgi_app = app.as_wsgi()
        status, headers, body, _ = _drive(wsgi_app, _get("/openapi.json"))
        assert status.startswith("200 ")
        assert ("content-type", "application/json") in headers
        doc = json.loads(body)
        assert doc["openapi"].startswith("3.")
        assert "/hello" in doc["paths"]

    def test_serves_swagger_ui(self):
        app = HttpApp()
        wsgi_app = app.as_wsgi()
        status, headers, body, _ = _drive(wsgi_app, _get("/docs"))
        assert status.startswith("200 ")
        assert any(k == "content-type" and "text/html" in v for k, v in headers)
        assert b"swagger-ui" in body.lower()


class TestHttpAppAsWsgiStreaming:
    def test_sse_generator_streams_lazily(self):
        consumed: list[int] = []

        app = HttpApp()

        @app.get("/feed")
        def feed() -> Iterator[Event[str]]:
            for i in range(3):
                consumed.append(i)
                yield Event(data=f"event-{i}")

        wsgi_app = app.as_wsgi()
        environ = _get("/feed")

        captured: dict[str, Any] = {}

        def start_response(status: str, headers: list[tuple[str, str]], exc_info: Any = None) -> Any:
            captured["status"] = status
            captured["headers"] = headers
            return lambda _: None

        body_iter = wsgi_app(environ, start_response)
        # Pre-iteration: lazy — only what to_wsgi needed to materialise
        # (which is nothing — stream() captures the iterator and returns it).
        assert consumed == []
        # Drain — chunks come out one at a time as the WSGI host iterates.
        chunks = list(body_iter)
        assert consumed == [0, 1, 2]
        assert captured["status"].startswith("200 ")
        assert any(k == "content-type" and "text/event-stream" in v for k, v in captured["headers"])
        joined = b"".join(chunks)
        assert b"data: event-0" in joined
        assert b"data: event-2" in joined
