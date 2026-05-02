# localpost.openapi

Type-driven HTTP framework with built-in **OpenAPI 3.2** generation, on top of
[`localpost.http`](../http/README.md). FastAPI-inspired; the differences:

- **Response shapes are union return types**, not a separate `response_model=`
  declaration:
  ```python
  def get_book(book_id: str) -> Book | NotFound[str]: ...
  ```
  Both branches end up in the OpenAPI doc with proper schemas; the runtime
  short-circuits to the right status code.
- **OpenAPI-aware middleware (filters).** Filters and auth contribute to the
  spec themselves (security schemes, extra parameters, extra response codes)
  via an `update_doc` hook. There's no second place to declare auth.
- **msgspec-first.** [msgspec](https://jcristharif.com/msgspec/) does request
  body decoding, response encoding, and JSON Schema generation. Pydantic
  models are recognised automatically — but pydantic is **not** a runtime
  dependency; install it yourself if you want to use it.

## Install

```bash
pip install 'localpost[http,openapi]'
```

For Pydantic models in handlers:

```bash
pip install pydantic
```

## Quick start

```python
import sys
from dataclasses import dataclass

from localpost import hosting
from localpost.http import ServerConfig
from localpost.openapi import HttpApp, NotFound, BadRequest, Created


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
def get_book(book_id: str) -> Book | NotFound[str]:
    if book_id != "42":
        return NotFound(f"Book not found: {book_id}")
    return Book(id=book_id, title="HHGTTG", author="Adams")


@app.post("/books")
def create_book(book: Book) -> Created[Book]:
    return Created(book, headers={"Location": f"/books/{book.id}"})


if __name__ == "__main__":
    sys.exit(hosting.run_app(app.service(ServerConfig(port=8000))))
```

```bash
curl http://localhost:8000/hello/world
curl http://localhost:8000/openapi.json   # OpenAPI 3.2 doc
open http://localhost:8000/docs           # Swagger UI
open http://localhost:8000/docs/redoc     # ReDoc
open http://localhost:8000/docs/scalar    # Scalar
```

## Concepts

### Operations

Plain Python functions registered with `@app.get(path)`, `.post`, `.put`,
`.delete`, `.patch`. The path follows
[`URITemplate`](../http/README.md) syntax (`/books/{id}`).

### Argument resolvers

One per parameter. Picked from the annotation; explicit factories override:

| Source | Factory | Auto-picked when… |
|---|---|---|
| Path variable | `FromPath()` | param name matches `{name}` in template |
| Query string | `FromQuery()` | scalar parameter not in path, no body type |
| Header | `FromHeader("X-…")` | only via explicit `Annotated[...]` |
| Request body | `FromBody()` | param annotated as `msgspec.Struct` / dataclass / pydantic model |
| Request ctx | (none) | param annotated as `HTTPReqCtx` |

Each resolver may short-circuit by returning an `OpResult` (e.g. validation
failure → `BadRequest`).

### `OpResult` hierarchy

| Class | Status | Notes |
|---|---|---|
| `Ok[T]` | 200 | Implicit when you return a plain value. |
| `Created[T]` | 201 | |
| `Accepted[T]` | 202 | |
| `NoContent` | 204 | No body. |
| `BadRequest[T]` | 400 | |
| `Unauthorized[T]` | 401 | |
| `Forbidden[T]` | 403 | |
| `NotFound[T]` | 404 | |
| `Conflict[T]` | 409 | |
| `UnprocessableEntity[T]` | 422 | |
| `TooManyRequests[T]` | 429 | |
| `InternalServerError[T]` | 500 | |

Use the *class* in return annotations (`Book | NotFound[str]`) so the OpenAPI
doc picks up the body type per status code. Returning a bare
`localpost.http.Response` is the escape hatch (no schema contribution).

### `OpFilter` (designed; concrete impls land later)

```python
class OpFilter(Protocol):
    def __call__(self, ctx: HTTPReqCtx, /) -> None | OpResult: ...
    def update_doc(
        self, doc: spec.OpenAPI, op: spec.Operation | None = None, /
    ) -> spec.OpenAPI: ...
```

App-level filters get `op=None`; per-operation filters get the relevant
`Operation`. `update_doc` returns a new (immutable) doc. Concrete
`HttpBasicAuth` / `HttpBearerAuth` / `OpenIDConnectAuth` are a follow-up.

## Hosting

`HttpApp.service(config)` returns a `localpost.hosting.service` you feed to
`hosting.run_app(...)` or `hosting.serve(...)`. It composes:

1. A worker pool (`thread_pool_handler`) so user fns run on threads, not the
   selector.
2. The HTTP server (`http_server`).

Pass `selectors=N` and/or `acceptor=True` to use multi-selector topology.

## Design notes

See the in-tree design doc for rationale and a layout map: keep this README
focused on user-facing concepts.

## Sub-modules

| Module | Provides |
|---|---|
| `app.py` | `HttpApp`, registration, hosting entrypoint, built-in `/openapi.json` + `/docs` UIs |
| `operation.py` | `Operation`: signature parsing, return-type inference, runtime closure |
| `resolvers.py` | `FromPath`, `FromQuery`, `FromHeader`, `FromBody` factories |
| `results.py` | `OpResult` hierarchy |
| `filter.py` | `OpFilter` protocol |
| `spec.py` | OpenAPI 3.2 dataclasses |
| `schemas.py` | `SchemaRegistry` — msgspec / pydantic JSON Schema generation |
| `pydantic.py` | Explicit pydantic helpers (auto-detection in `FromBody` works without this) |
| `_docs.py` | HTML for Swagger UI / ReDoc / Scalar (CDN-loaded) |
