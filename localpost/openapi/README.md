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

### `OpFilter`

A filter is middleware that knows how to describe itself in the OpenAPI doc:

```python
class OpFilter(Protocol):
    def __call__(self, ctx: HTTPReqCtx, /) -> None | OpResult: ...
    def contribute_root(self, doc: spec.OpenAPI, registry: SchemaRegistry, /) -> spec.OpenAPI: ...
    def contribute_operation(self, op: spec.Operation, registry: SchemaRegistry, /) -> spec.Operation: ...
```

- `__call__` runs before the resolvers; return an `OpResult` to short-circuit.
- `contribute_root` is called once at spec-build time (typically to register
  a `SecurityScheme`).
- `contribute_operation` is called for each operation that the filter is
  attached to (typically to add a 401 response and a `security` requirement).

Apply app-wide:

```python
app = HttpApp(filters=[my_filter])
```

or per-operation:

```python
@app.get("/admin", filters=[my_filter])
def admin() -> str: ...
```

### Server-Sent Events (SSE)

Return a generator (or any iterator) and the operation auto-promotes to an
SSE stream — `Content-Type: text/event-stream`, chunked transfer encoding,
one event per yielded value:

```python
from collections.abc import Generator
from dataclasses import dataclass

from localpost.openapi import HttpApp, Event


@dataclass
class Tick:
    n: int


app = HttpApp()


@app.get("/clock")
def clock() -> Generator[Event[Tick]]:
    for n in range(60):
        yield Event(data=Tick(n=n), id=str(n))
        time.sleep(1)
```

Yield bare values for `data:`-only events; yield `Event` instances for
control over `event:` / `id:` / `retry:` / comment fields. The OpenAPI doc
emits `text/event-stream` (with the schema for `Event[Tick]`) instead of
`application/json` for that operation.

Cancellation: the framework calls `localpost.http.check_cancelled` between
events, so client disconnects (and pool shutdowns) terminate the stream
without leaking workers — provided the app runs under
`thread_pool_handler` (the default for `HttpApp.service(...)`).

For an iterator you've already constructed, wrap it in `EventStream(...)`
to be explicit; otherwise just return the generator directly.

### Built-in auth filters

```python
from localpost.openapi import HttpApp, HttpBearerAuth, HttpBasicAuth

def validate_token(token: str) -> dict | None:
    # Return any truthy principal on success, None on failure.
    # The principal is stashed on ctx.attrs[<filter>] for handlers to read.
    return decode_jwt(token)


app = HttpApp(filters=[HttpBearerAuth(validator=validate_token)])


@app.get("/me")
def me() -> dict:
    # Pull the principal from a custom resolver, or just inject the ctx.
    ...
```

`HttpBearerAuth` registers an HTTP `bearer` security scheme; `HttpBasicAuth`
registers `basic` and sends a `WWW-Authenticate` challenge on 401. Both
attach a 401 response and a `security` requirement to every operation they
cover. `OpenIDConnectAuth` is a follow-up.

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
| `auth.py` | `HttpBearerAuth`, `HttpBasicAuth` — concrete auth filters |
| `sse.py` | `Event`, `EventStream`, encoder — Server-Sent Events |
| `spec.py` | OpenAPI 3.2 dataclasses |
| `schemas.py` | `SchemaRegistry` — msgspec / pydantic JSON Schema generation |
| `pydantic.py` | Explicit pydantic helpers (auto-detection in `FromBody` works without this) |
| `_docs.py` | HTML for Swagger UI / ReDoc / Scalar (CDN-loaded) |
