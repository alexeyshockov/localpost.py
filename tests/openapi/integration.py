"""End-to-end HTTP tests against a real ``localpost.openapi.HttpApp``.

These spin up the server in-thread (no worker pool — see
``tests/openapi/conftest.py``) and exercise it with httpx. They cover the
plumbing tests in ``tests/openapi/app.py`` can't reach: actual socket
I/O, the built-in ``/openapi.json`` and ``/docs`` routes, and round-trip
serialisation of the OpenAPI 3.2 doc.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated

import httpx
import pytest

from localpost.openapi import (
    BadRequest,
    Created,
    FromHeader,
    HttpApp,
    NotFound,
    spec,
)


@dataclass
class Book:
    id: str
    title: str


@pytest.fixture
def library_app() -> HttpApp:
    app = HttpApp(info=spec.Info(title="Library API", version="1.0.0"))
    library: dict[str, Book] = {
        "42": Book(id="42", title="HHGTTG"),
    }

    @app.get("/hello/{name}")
    def hello(name: str) -> str | BadRequest[str]:
        if name.lower() == "donald":
            return BadRequest("Sorry, you are not welcome here")
        return f"Hello, {name}!"

    @app.get("/books/{book_id}")
    def get_book(book_id: str) -> Book | NotFound[str]:
        book = library.get(book_id)
        if book is None:
            return NotFound(f"Book not found: {book_id}")
        return book

    @app.post("/books")
    def create_book(book: Book) -> Created[Book]:
        library[book.id] = book
        return Created(book, headers={"Location": f"/books/{book.id}"})

    @app.get("/me")
    def me(user_id: Annotated[str, FromHeader("X-User-Id")]) -> str:
        return user_id

    return app


class TestPathAndQuery:
    def test_hello_world(self, serve_app, library_app):
        with serve_app(library_app) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/hello/world", timeout=5)
        assert resp.status_code == 200
        assert resp.text == "Hello, world!"
        assert resp.headers["content-type"].startswith("text/plain")

    def test_bad_request_short_circuit(self, serve_app, library_app):
        with serve_app(library_app) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/hello/Donald", timeout=5)
        assert resp.status_code == 400
        assert resp.text == "Sorry, you are not welcome here"


class TestOpResultUnion:
    def test_200_branch(self, serve_app, library_app):
        with serve_app(library_app) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/books/42", timeout=5)
        assert resp.status_code == 200
        assert resp.json() == {"id": "42", "title": "HHGTTG"}

    def test_404_branch(self, serve_app, library_app):
        with serve_app(library_app) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/books/missing", timeout=5)
        assert resp.status_code == 404
        assert resp.text == "Book not found: missing"


class TestRequestBody:
    def test_json_body_and_created_response(self, serve_app, library_app):
        with serve_app(library_app) as port:
            resp = httpx.post(
                f"http://127.0.0.1:{port}/books",
                json={"id": "7", "title": "Dune"},
                timeout=5,
            )
        assert resp.status_code == 201
        assert resp.headers["Location"] == "/books/7"
        assert resp.json() == {"id": "7", "title": "Dune"}

    def test_invalid_body(self, serve_app, library_app):
        with serve_app(library_app) as port:
            resp = httpx.post(
                f"http://127.0.0.1:{port}/books",
                content=b"not json",
                headers={"Content-Type": "application/json"},
                timeout=5,
            )
        assert resp.status_code == 400
        assert b"Invalid request body" in resp.content


class TestHeaderResolver:
    def test_header_present(self, serve_app, library_app):
        with serve_app(library_app) as port:
            resp = httpx.get(
                f"http://127.0.0.1:{port}/me",
                headers={"X-User-Id": "alice"},
                timeout=5,
            )
        assert resp.status_code == 200
        assert resp.text == "alice"

    def test_missing_header(self, serve_app, library_app):
        with serve_app(library_app) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/me", timeout=5)
        assert resp.status_code == 400
        assert b"Missing required header" in resp.content


class TestBuiltInRoutes:
    def test_openapi_json_served(self, serve_app, library_app):
        with serve_app(library_app) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/openapi.json", timeout=5)
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/json"
        doc = resp.json()
        assert doc["openapi"] == "3.2.0"
        assert doc["info"]["title"] == "Library API"
        assert "/hello/{name}" in doc["paths"]
        assert "/books/{book_id}" in doc["paths"]
        # Per-status response schemas land in the doc.
        responses = doc["paths"]["/books/{book_id}"]["get"]["responses"]
        assert set(responses) == {"200", "404"}
        # Components for named types.
        assert "Book" in doc["components"]["schemas"]

    def test_swagger_ui_served(self, serve_app, library_app):
        with serve_app(library_app) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/docs", timeout=5)
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/html; charset=utf-8"
        assert b"Swagger UI" in resp.content

    def test_redoc_served(self, serve_app, library_app):
        with serve_app(library_app) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/docs/redoc", timeout=5)
        assert resp.status_code == 200
        assert b"redoc" in resp.content.lower()

    def test_scalar_served(self, serve_app, library_app):
        with serve_app(library_app) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/docs/scalar", timeout=5)
        assert resp.status_code == 200
        assert b"scalar" in resp.content.lower()


class TestOpenAPIDocStructure:
    def test_openapi_doc_is_valid_json(self, serve_app, library_app):
        with serve_app(library_app) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/openapi.json", timeout=5)
        # Round-trip check.
        doc = json.loads(resp.content)
        assert doc["openapi"].startswith("3.2")
