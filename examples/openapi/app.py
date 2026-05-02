"""Working example for ``localpost.openapi.HttpApp``.

Run::

    uv run python examples/openapi/app.py

Then::

    curl http://localhost:8000/hello/world
    curl http://localhost:8000/books/42
    curl -X POST http://localhost:8000/books \\
        -H 'Content-Type: application/json' \\
        -d '{"id": "1", "title": "Dune", "author": "Frank Herbert"}'

Docs UIs:
    http://localhost:8000/docs        (Swagger UI)
    http://localhost:8000/docs/redoc  (ReDoc)
    http://localhost:8000/docs/scalar (Scalar)
"""

import sys
from dataclasses import dataclass

from localpost import hosting
from localpost.http import ServerConfig
from localpost.openapi import BadRequest, Created, HttpApp, NotFound, spec


@dataclass
class Book:
    id: str
    title: str
    author: str


_LIBRARY: dict[str, Book] = {
    "42": Book(id="42", title="The Hitchhiker's Guide to the Galaxy", author="Douglas Adams"),
}


app = HttpApp(info=spec.Info(title="Library API", version="1.0.0"))


@app.get("/hello/{name}")
def hello(name: str) -> str | BadRequest[str]:
    """Greet someone."""
    if name.lower() == "donald":
        return BadRequest("Sorry, you are not welcome here")
    return f"Hello, {name}!"


@app.get("/books/{book_id}")
def get_book(book_id: str) -> Book | NotFound[str]:
    """Look up a book by ID."""
    book = _LIBRARY.get(book_id)
    if book is None:
        return NotFound(f"Book not found: {book_id}")
    return book


@app.post("/books")
def create_book(book: Book) -> Created[Book]:
    """Add a new book to the library."""
    _LIBRARY[book.id] = book
    return Created(book, headers={"Location": f"/books/{book.id}"})


if __name__ == "__main__":
    sys.exit(hosting.run_app(app.service(ServerConfig(host="127.0.0.1", port=8000))))
