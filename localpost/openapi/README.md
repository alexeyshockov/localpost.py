# localpost.openapi

Type-driven HTTP framework with built-in **OpenAPI 3.2** generation, on top of
[`localpost.http`](../http/README.md). FastAPI-inspired; response shapes are
union return types (`Book | NotFound[str]`), middleware is OpenAPI-aware
(security schemes and extra responses contributed by the middleware itself),
and msgspec drives encoding / decoding / schema generation. Pydantic and
`attrs` classes are recognised automatically when present.

```bash
pip install 'localpost[http,openapi]'
```

```python
import sys
from dataclasses import dataclass
from localpost import hosting
from localpost.http import ServerConfig
from localpost.openapi import HttpApp, NotFound


@dataclass
class Book:
    id: str; title: str


app = HttpApp()


@app.get("/books/{book_id}")
def get_book(book_id: str) -> Book | NotFound[str]:
    if book_id != "42":
        return NotFound(f"Book not found: {book_id}")
    return Book(id=book_id, title="HHGTTG")


if __name__ == "__main__":
    sys.exit(hosting.run_app(app.service(ServerConfig(port=8000))))
```

`/openapi.json` plus `/docs` (Swagger UI), `/docs/redoc`, `/docs/scalar` are
served automatically.

**Full reference:** <https://alexeyshockov.github.io/localpost.py/modules/openapi/>

Examples: [`examples/openapi/`](../../examples/openapi/).
