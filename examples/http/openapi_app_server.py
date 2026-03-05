from wsgiref.simple_server import make_server

from localpost.http.openapi.app import HttpApp


def main():
    app = HttpApp()

    @app.get("/hello/{name}")
    def hello(name: str) -> str:
        return f"Hello, {name}!"

    @app.get("/books/{book_id}")
    def get_book(book_id: str, page_number: int = 1) -> dict:
        return {"id": book_id, "title": "The Lord of the Rings", "author": "J.R.R. Tolkien", "page": page_number}

    print("Starting server on http://localhost:8000")
    print("Try: curl http://localhost:8000/hello/world")
    print("Try: curl http://localhost:8000/openapi.json")
    with make_server("", 8000, app.wsgi) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
