"""Working example for ``localpost.openapi.HttpAsyncApp`` (async / ASGI).

Mirrors ``examples/openapi/app.py`` route-for-route, but every handler
is ``async def`` and the deployment target is ASGI (uvicorn here; same
ASGI app works under hypercorn or ``granian --interface asgi``).

Run::

    uv run python examples/openapi/async_app.py

Then::

    curl http://localhost:8000/hello/world
    curl http://localhost:8000/books/42
    curl -X POST http://localhost:8000/books \\
        -H 'Content-Type: application/json' \\
        -H 'X-API-Key: secret' \\
        -d '{"id": "1", "title": "Dune", "author": "Frank Herbert"}'
    curl -N http://localhost:8000/books/42/pages   # streams as SSE

Docs UIs:
    http://localhost:8000/docs        (Swagger UI)
    http://localhost:8000/docs/redoc  (ReDoc)
    http://localhost:8000/docs/scalar (Scalar)
"""

import asyncio
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

import uvicorn

from localpost.openapi import (
    AsyncApiOperation,
    AsyncHTTPReqCtx,
    BadRequest,
    Created,
    Event,
    FromHeader,
    HttpAsyncApp,
    OpResult,
    TooManyRequests,
    Unauthorized,
    async_op_middleware,
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


app = HttpAsyncApp(info=spec.Info(title="Library API (async)", version="1.0.0"))


# A middleware-as-a-tiny-operation: same shape as the sync flavour, but
# ``async def`` and built with ``@async_op_middleware``. The framework
# still reads the input header and the failure response from the
# signature and threads them into every operation that uses it.
@async_op_middleware
async def require_api_key(
    ctx: AsyncHTTPReqCtx,
    call_next: AsyncApiOperation,
    x_api_key: Annotated[str, FromHeader("X-API-Key")] = "",
) -> Unauthorized[str] | OpResult:
    if x_api_key != "secret":
        return Unauthorized("Invalid or missing X-API-Key")
    return await call_next(ctx)


# Every-N-requests rate limiter, just for the demo.
_call_counter = {"n": 0}


@async_op_middleware
async def rate_limit(
    ctx: AsyncHTTPReqCtx,
    call_next: AsyncApiOperation,
) -> TooManyRequests[str] | OpResult:
    _call_counter["n"] += 1
    if _call_counter["n"] % 10 == 0:
        return TooManyRequests("Slow down")
    return await call_next(ctx)


@app.get("/hello/{name}")
async def hello(name: str) -> str | BadRequest[str]:
    """Greet someone."""
    if name.lower() == "donald":
        return BadRequest("Sorry, you are not welcome here")
    return f"Hello, {name}!"


@app.get("/books/{book_id}")
async def get_book(book_id: str) -> Book | None:
    """Look up a book by ID.

    ``Book | None`` is the implicit "a book or NotFound" — the framework
    promotes a ``None`` return into a 404, and the OpenAPI doc declares
    both branches.
    """
    return _LIBRARY.get(book_id)


@app.post("/books", middlewares=[require_api_key, rate_limit])
async def create_book(book: Book) -> Created[Book]:
    """Add a new book to the library.

    Requires ``X-API-Key: secret``; subject to a (silly) every-10th-request
    rate limit. Both middlewares' input headers and failure responses
    appear in the OpenAPI doc automatically.
    """
    _LIBRARY[book.id] = book
    return Created(book, headers={"Location": f"/books/{book.id}"})


@app.get("/books/{book_id}/pages")
async def stream_pages(book_id: str) -> AsyncIterator[Event[BookPage]]:
    """Stream the book's pages as Server-Sent Events.

    The ``AsyncIterator`` return type auto-promotes to ``text/event-stream``.
    ``HttpAsyncApp`` rejects sync generators here — use ``async def``
    generators so I/O between yields stays on the event loop.
    """
    book = _LIBRARY.get(book_id)
    if book is None:
        # Same caveat as the sync example: SSE handlers can only yield
        # events; "not found" surfaces as a single error event.
        yield Event(data=BookPage(book_id=book_id, number=0, content="not found"), event="error")
        return
    for n in range(1, 6):
        yield Event(data=BookPage(book_id=book_id, number=n, content=f"page {n} of {book.title}"), id=str(n))
        await asyncio.sleep(0.5)


def main() -> int:
    # ``app.asgi()`` returns a plain ASGI 3 callable — works under any
    # ASGI server. For ``localpost.hosting`` integration use
    # ``app.service(uvicorn.Config(...))`` instead and feed it to
    # ``hosting.run_app(...)``; that wires uvicorn into the lifecycle
    # alongside other LocalPost services.
    uvicorn.run(app.asgi(), host="127.0.0.1", port=8000, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
