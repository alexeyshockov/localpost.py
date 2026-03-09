import random
from collections.abc import Generator, Iterator
from dataclasses import dataclass
from typing import Annotated, Literal
from wsgiref.simple_server import make_server

from localpost.http.openapi.app import BadRequest, FromPath, HttpApp, NotFound, TooManyRequests


@dataclass()
class Book:
    id: str
    title: str
    author: str


@dataclass()
class BookPage:
    book_id: str
    number: int
    content: str


@dataclass()
class User:
    name: str
    role: Literal["admin", "user"]


@op_filter
def limit_requests(client_id) -> None | TooManyRequests[str]:
    if random.random() < 0.5:
        return TooManyRequests("Too many requests", headers={"Retry-After": "1"})
    return None


def get_book(book_id: str, req, param) -> Book | NotFound[str]:
    if book_id != "00a7a2d4-18e4-11f1-899b-d33838f3bef0":
        return NotFound(f"Book not found: {book_id}")
    return Book(id=book_id, title="The Lord of the Rings", author="J.R.R. Tolkien")


def main():
    app = HttpApp()

    @app.get("/hello/{name}")
    def hello(name: str) -> str | BadRequest[str]:
        if name.lower() == "donald":
            return BadRequest("Sorry, you are not welcome here")

        return f"Hello, {name}!"

    @limit_requests()
    @app.get("/books/{book_id}")
    def get_book(book_id: str, page_number: int = 1) -> Book:
        return Book(id=book_id, title="The Lord of the Rings", author="J.R.R. Tolkien")

    @app.get("/books/{book_id}/num_pages")
    def count_pages(book_id: Annotated[Book, FromPath()]) -> int:
        return 377

    # A generator is automatically transformed into an EventStream response (SSE),
    # so Generator[BookPage] = Ok(EventStream[BookPage])
    @app.get("/books/{book_id}/pages")
    def get_book_pages(book_id: str) -> Generator[BookPage]:
        for page_number in range(1, 378):
            yield BookPage(book_id=book_id, number=page_number, content="...")

    print("Starting server on http://localhost:8000")
    print("Try: curl http://localhost:8000/hello/world")
    print("Try: curl http://localhost:8000/openapi.json")
    print("Docs: http://localhost:8000/docs (Swagger UI)")
    print("Docs: http://localhost:8000/docs/redoc (ReDoc)")
    print("Docs: http://localhost:8000/docs/scalar (Scalar)")
    with make_server("", 8000, app.wsgi) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
