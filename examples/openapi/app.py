"""Working example for ``localpost.openapi.HttpApp``.

Run::

    uv run python examples/openapi/app.py

Then::

    curl http://localhost:8000/hello/world
    curl http://localhost:8000/books/42
    curl -X POST http://localhost:8000/books \\
        -H 'Content-Type: application/json' \\
        -d '{"id": "1", "title": "Dune", "author": "Frank Herbert"}'
    curl -N http://localhost:8000/books/42/pages   # streams as SSE

Docs UIs:
    http://localhost:8000/docs        (Swagger UI)
    http://localhost:8000/docs/redoc  (ReDoc)
    http://localhost:8000/docs/scalar (Scalar)
"""

import time
from collections.abc import Generator
from dataclasses import dataclass
from typing import Annotated

from localpost import hosting
from localpost.http import HTTPReqCtx, ServerConfig
from localpost.openapi import (
    ApiOperation,
    BadRequest,
    Created,
    Event,
    FromHeader,
    HttpApp,
    OpResult,
    TooManyRequests,
    Unauthorized,
    op_middleware,
    spec,
)


@dataclass
class Book:
    id: str
    title: str
    author: str


@dataclass
class BookPage:
    book_id: str
    number: int
    content: str


_LIBRARY: dict[str, Book] = {
    "42": Book(id="42", title="The Hitchhiker's Guide to the Galaxy", author="Douglas Adams"),
}


app = HttpApp(info=spec.Info(title="Library API", version="1.0.0"))


# A middleware as a tiny operation: the framework reads the input
# (``X-API-Key`` header) and the failure response (``Unauthorized``) from
# this signature and threads them into every operation that uses it.
@op_middleware
def require_api_key(
    ctx: HTTPReqCtx,
    call_next: ApiOperation,
    x_api_key: Annotated[str, FromHeader("X-API-Key")] = "",
) -> Unauthorized[str] | OpResult:
    if x_api_key != "secret":
        return Unauthorized("Invalid or missing X-API-Key")
    return call_next(ctx)


# Another tiny middleware — every-N-requests rate limiter, just for the demo.
_call_counter = {"n": 0}


@op_middleware
def rate_limit(
    ctx: HTTPReqCtx,
    call_next: ApiOperation,
) -> TooManyRequests[str] | OpResult:
    _call_counter["n"] += 1
    if _call_counter["n"] % 10 == 0:
        return TooManyRequests("Slow down")
    return call_next(ctx)


@app.get("/hello/{name}")
def hello(name: str) -> str | BadRequest[str]:
    """Greet someone."""
    if name.lower() == "donald":
        return BadRequest("Sorry, you are not welcome here")
    return f"Hello, {name}!"


@app.get("/books/{book_id}")
def get_book(book_id: str) -> Book | None:
    """Look up a book by ID."""
    book = _LIBRARY.get(book_id)
    # Optional[Book] is treated like "a book or NotFound"
    return book


@app.post("/books", middlewares=[require_api_key, rate_limit])
def create_book(book: Book) -> Created[Book]:
    """Add a new book to the library.

    Requires ``X-API-Key: secret``; subject to a (silly) every-10th-request
    rate limit. Both middlewares' input headers and failure responses
    appear in the OpenAPI doc automatically — no spec annotations needed
    here.
    """
    _LIBRARY[book.id] = book
    return Created(book, headers={"Location": f"/books/{book.id}"})


@app.get("/books/{book_id}/pages")
def stream_pages(book_id: str) -> Generator[Event[BookPage]]:
    """Stream the book's pages as Server-Sent Events.

    The Generator return type auto-promotes to ``text/event-stream``.
    """
    book = _LIBRARY.get(book_id)
    if book is None:
        # SSE handlers can only return events; surface "not found" as a
        # single error event rather than a 404. (Mid-stream switching to
        # JSON requires returning before the first yield — different
        # design.)
        yield Event(data=BookPage(book_id=book_id, number=0, content="not found"), event="error")
        return
    for n in range(1, 6):
        yield Event(data=BookPage(book_id=book_id, number=n, content=f"page {n} of {book.title}"), id=str(n))
        time.sleep(0.5)


if __name__ == "__main__":
    hosting.run_app(app.service(ServerConfig(host="127.0.0.1", port=8000)))
