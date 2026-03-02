import logging
from typing import Annotated
from uuid import UUID

from localpost import threadtools
from localpost.http.config import ServerConfig
from localpost.http.openapi.app import HttpApp, Example
from localpost.http.server import start_http_server


# noinspection DuplicatedCode
def main():
    logging.basicConfig(level=logging.DEBUG)
    threadtools.check_cancelled = lambda: None

    app = HttpApp()

    @app.get("/hello/{name}")
    def hello(name: str) -> str:
        return f"Hello, {name}!"

    @app.get("/books/{book_id}")
    def hello(
        book_id: UUID, page_number: int
    ) -> Annotated[
        dict,
        Example(
            {"id": "123e4567-e89b-12d3-a456-426614174000", "title": "The Lord of the Rings", "author": "J.R.R. Tolkien"}
        ),
    ]:
        # Dict (or any other type, actually) is serialized to JSON (or other format, according to the Accept header)
        # page_number is just to demonstrate multiple parameters from different sources (path and query)
        return {"id": str(id), "title": "The Lord of the Rings", "author": "J.R.R. Tolkien"}

    with start_http_server(ServerConfig()) as server:
        while True:
            server.run(app)


if __name__ == "__main__":
    main()
