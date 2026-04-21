# localpost.openapi

> **Status:** experimental — API surface is still being shaped; expect breaking changes before `1.0`.

A type-driven OpenAPI 3.0 framework sitting on top of [`localpost.http`](../http/README.md).

Define operations as ordinary Python functions. Types, annotations, and return
types are inspected to build the OpenAPI doc and to wire path / query / header /
body resolvers — no schemas to hand-write. The generated spec is served at
`/openapi.json`, with three doc UIs built in.

For deeper design notes see [`DESIGN.md`](DESIGN.md).

## Install

```bash
pip install localpost[http-server,http-openapi]
```

The OpenAPI layer uses [msgspec](https://jcristharif.com/msgspec/) by default
for schema / JSON handling; Pydantic converters are also provided.

## Quick start

```python
from collections.abc import Generator
from dataclasses import dataclass
from typing import Annotated
from wsgiref.simple_server import make_server

from localpost.openapi.app import BadRequest, FromPath, HttpApp, NotFound


@dataclass
class Book:
    id: str
    title: str
    author: str


app = HttpApp()


@app.get("/hello/{name}")
def hello(name: str) -> str | BadRequest[str]:
    if name.lower() == "donald":
        return BadRequest("Sorry, you are not welcome here")
    return f"Hello, {name}!"


@app.get("/books/{book_id}")
def get_book(book_id: str, page_number: int = 1) -> Book | NotFound[str]:
    if book_id != "00a7a2d4-18e4-11f1-899b-d33838f3bef0":
        return NotFound(f"Book not found: {book_id}")
    return Book(id=book_id, title="The Lord of the Rings", author="J.R.R. Tolkien")


if __name__ == "__main__":
    # Try:
    #   curl http://localhost:8000/hello/world
    #   curl http://localhost:8000/openapi.json
    # Docs:
    #   http://localhost:8000/docs        (Swagger UI)
    #   http://localhost:8000/docs/redoc  (ReDoc)
    #   http://localhost:8000/docs/scalar (Scalar)
    with make_server("", 8000, app.wsgi) as server:
        server.serve_forever()
```

Full example: [`examples/openapi/app.py`](../../examples/openapi/app.py).

## Key concepts

- **`HttpApp`** — the root object. Holds the router, the OpenAPI doc, and
  registered operations. Exposes `.wsgi` for any WSGI host (including
  `localpost.http.wsgi_server`).
- **Operations** — Python functions registered with `@app.get(path)`,
  `.post`, etc. The path follows [`URITemplate`](../http/README.md) syntax
  (`/books/{id}`).
- **Arg resolvers** — one per parameter, picked from the annotation. Factories:
  - `FromPath()` — path variable (auto-detected for params whose name matches a
    `{name}` in the template).
  - `FromQuery()` — query string (default for unannotated params that aren't
    in the path).
  - `FromHeader("Name")`
  - `FromBody(converter=...)` — e.g. `PydanticBodyConverter`.
  Any resolver can short-circuit by returning an `OpResult`.
- **`OpResult` hierarchy** — `Ok`, `BadRequest[T]`, `Unauthorized[T]`,
  `NotFound[T]`, `TooManyRequests[T]`, etc. Return one from your function to
  emit a non-200 response; the OpenAPI spec learns about it from the return
  type annotation.
- **Result converters** — turn the function's return value into HTTP bytes.
  Pluggable via `ResultConverterFactory`. Pydantic support is in
  [`pydantic.py`](pydantic.py); msgspec handling is the default.
- **Immutable spec** — every registration produces a new OpenAPI doc; the doc
  itself is a tree of frozen dataclasses from [`spec.py`](spec.py).

## Public API

From `localpost.openapi.app`:

| Symbol              | Purpose                                             |
| ------------------- | --------------------------------------------------- |
| `HttpApp`           | Root app; `.get`, `.post`, `.put`, `.delete`, `.wsgi`, `.openapi_doc` |
| `FromPath()`        | Arg resolver factory — path variable                |
| `FromQuery()`       | Arg resolver factory — query string                 |
| `FromHeader(name)`  | Arg resolver factory — header                       |
| `FromBody(converter)` | Arg resolver factory — body                       |
| `Ok[T]`             | Success wrapper (usually implicit)                  |
| `BadRequest[T]`, `Unauthorized[T]`, `NotFound[T]`, `TooManyRequests[T]` | `OpResult` subclasses |
| `OpFilter` (Protocol) | Per-operation pre-filter (e.g. `limit_requests`)  |

From `localpost.openapi.spec`:

| Symbol               | Notes                                               |
| -------------------- | --------------------------------------------------- |
| `OpenAPI`, `Info`, `Operation`, `Response`, `MediaType`, … | Immutable spec dataclasses |

## Sub-modules

| Module         | What it provides                                                        |
| -------------- | ----------------------------------------------------------------------- |
| `app.py`       | `HttpApp`, arg resolvers, `OpResult` hierarchy, `FluentPathOp`          |
| `spec.py`      | OpenAPI 3.0 spec dataclasses                                            |
| `converters.py`| Generic body / result converter helpers                                 |
| `pydantic.py`  | `PydanticBodyConverter`, `PydanticResultConverter` — use if Pydantic is your SoT |
| `msgspec.py`   | msgspec converters (stub; msgspec is the default for schema handling)   |
| `sse.py`       | `Event[T]`, `EventStream[T]` for Server-Sent Events (a generator return type is auto-promoted to SSE) |
| `_docs.py`     | HTML templates for Swagger UI, ReDoc, Scalar (CDN-loaded)               |

## Writing a custom arg resolver

```python
from localpost.openapi.app import ArgResolverFactory
from localpost.http.router import RequestCtx

class FromCookie(ArgResolverFactory):
    def __init__(self, name: str):
        self.name = name

    def __call__(self, param):
        def resolve(req: RequestCtx):
            cookies = req.headers.get("cookie", "")
            # ...parse...
            return value
        return resolve
```

Use it:

```python
@app.get("/me")
def me(session: Annotated[str, FromCookie("session")]) -> str:
    return session
```

## See also

- Examples: [`examples/openapi/`](../../examples/openapi/)
- Design notes: [`DESIGN.md`](DESIGN.md)
- Underlying server: [`../http/README.md`](../http/README.md)
