"""Deploy ``localpost.openapi.HttpApp`` under a WSGI server (Gunicorn / uWSGI / Werkzeug).

Run with Gunicorn::

    uv pip install gunicorn
    uv run gunicorn examples.openapi.wsgi_app:app -b 127.0.0.1:8000

Or with the stdlib's reference WSGI server (no extra dep)::

    uv run python examples/openapi/wsgi_app.py

Then::

    curl http://localhost:8000/hello/world
    curl http://localhost:8000/openapi.json
    curl -N http://localhost:8000/feed   # SSE stream — chunks flushed per event

Notes:

- ``HttpApp.as_wsgi()`` returns a standard WSGI callable. Worker
  concurrency is the WSGI server's responsibility — ``thread_pool_handler``
  does not apply.
- SSE / streaming via ``ctx.stream`` is handed to the WSGI server as a
  lazy iterator: events flush per yield, no extra threads.
- ``check_cancelled()`` is a no-op under WSGI; a long-running stream
  surfaces client disconnects as ``BrokenPipeError`` on the next yield.
"""

from collections.abc import Iterator
from dataclasses import dataclass

from localpost.openapi import Event, HttpApp, NotFound


@dataclass
class Book:
    id: str
    title: str


_books: dict[str, Book] = {
    "42": Book(id="42", title="The Hitchhiker's Guide to the Galaxy"),
}


app = HttpApp()


@app.get("/hello/{name}")
def hello(name: str) -> str:
    return f"Hello, {name}!"


@app.get("/books/{book_id}")
def get_book(book_id: str) -> Book | NotFound[str]:
    book = _books.get(book_id)
    if book is None:
        return NotFound(f"book not found: {book_id}")
    return book


@app.get("/feed")
def feed() -> Iterator[Event[str]]:
    """Lazy SSE — each ``yield`` lands on the wire as the WSGI server iterates."""
    for i in range(5):
        yield Event(data=f"event-{i}")


# WSGI entry point — gunicorn examples.openapi.wsgi_app:wsgi_app
wsgi_app = app.as_wsgi()


if __name__ == "__main__":
    from wsgiref.simple_server import make_server

    with make_server("127.0.0.1", 8000, wsgi_app) as srv:
        print("Serving on http://127.0.0.1:8000  (Ctrl-C to stop)")
        srv.serve_forever()
