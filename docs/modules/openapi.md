# localpost.openapi

Type-driven HTTP framework with built-in **OpenAPI 3.2** generation, on top of
[`localpost.http`](http.md). FastAPI-inspired; the differences:

- **Response shapes are union return types**, not a separate `response_model=`
  declaration:
  ```python
  def get_book(book_id: str) -> Book | NotFound[str]: ...
  ```
  Both branches end up in the OpenAPI doc with proper schemas; the runtime
  short-circuits to the right status code.
- **OpenAPI-aware middleware.** Middlewares and auth contribute to the
  spec themselves (security schemes, extra parameters, extra response codes)
  via an `update_doc` hook. There's no second place to declare auth.
- **msgspec-first.** [msgspec](https://jcristharif.com/msgspec/) does request
  body decoding, response encoding, and JSON Schema generation. Pydantic
  models *and* `attrs.define`'d classes are recognised automatically — but
  neither is a runtime dependency of the `openapi` extra; install them
  yourself if you want to use them.

## Install

```bash
pip install 'localpost[http,openapi]'
```

For Pydantic models in handlers:

```bash
pip install pydantic
```

For `attrs` classes in handlers (uses `cattrs` for structuring):

```bash
pip install 'localpost[openapi-attrs]'
# or, equivalently:
pip install attrs cattrs
```

## Quick start

```python
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
    hosting.run_app(app.service(ServerConfig(port=8000)))
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
[`URITemplate`](http.md) syntax (`/books/{id}`).

### Argument resolvers

One per parameter. Picked from the annotation; explicit factories override:

| Source | Factory | Auto-picked when… |
|---|---|---|
| Path variable | `FromPath()` | param name matches `{name}` in template |
| Query string | `FromQuery()` | scalar parameter not in path, no body type |
| Header | `FromHeader("X-…")` | only via explicit `Annotated[...]` |
| Request body | `FromBody()` | param annotated as `msgspec.Struct` / dataclass / pydantic model / `attrs` class |
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
| `EventStreamResult[T]` | 200 | SSE stream — see below. |
| `BadRequest[T]` | 400 | |
| `Unauthorized[T]` | 401 | |
| `Forbidden[T]` | 403 | |
| `NotFound[T]` | 404 | |
| `Conflict[T]` | 409 | |
| `UnprocessableEntity[T]` | 422 | |
| `TooManyRequests[T]` | 429 | |
| `InternalServerError[T]` | 500 | |

Use the *class* in return annotations (`Book | NotFound[str]`) so the OpenAPI
doc picks up the body type per status code.

### `OpMiddleware`

A middleware wraps the operation core. It receives the request context and
a `call_next` callable that runs the rest of the chain — and knows how to
describe itself in the OpenAPI doc:

```python
ApiOperation = Callable[[HTTPReqCtx], OpResult]


class OpMiddleware(Protocol):
    def __call__(self, ctx: HTTPReqCtx, call_next: ApiOperation, /) -> OpResult: ...
    def contribute_root(self, doc: spec.OpenAPI, registry: SchemaRegistry, /) -> spec.OpenAPI: ...
    def contribute_operation(self, op: spec.Operation, registry: SchemaRegistry, /) -> spec.Operation: ...
```

- `__call__` runs around the operation. Return `call_next(ctx)` to forward
  (optionally post-processing the result), or short-circuit by returning
  any other `OpResult`.
- `contribute_root` is called once at spec-build time (typically to register
  a `SecurityScheme`).
- `contribute_operation` is called for each operation that the middleware is
  attached to (typically to add a 401 response and a `security` requirement).

Apply app-wide:

```python
app = HttpApp(middlewares=[my_middleware])
```

or per-operation:

```python
@app.get("/admin", middlewares=[my_middleware])
def admin() -> str: ...
```

### `@op_middleware` — middlewares as tiny operations

For the common case, write a middleware the same way you'd write a handler:
declare your inputs (plus the `call_next` parameter), declare your possible
failure responses in the return type, and let the framework do the OpenAPI
bookkeeping:

```python
from typing import Annotated

from localpost.http import HTTPReqCtx
from localpost.openapi import (
    ApiOperation,
    FromHeader,
    OpResult,
    TooManyRequests,
    op_middleware,
)


@op_middleware
def rate_limit(
    ctx: HTTPReqCtx,
    call_next: ApiOperation,
    x_client: Annotated[str, FromHeader("X-Client-Id")],
) -> TooManyRequests[str] | OpResult:
    if quota_exhausted(x_client):
        return TooManyRequests("Slow down")
    return call_next(ctx)


@app.get("/expensive", middlewares=[rate_limit])
def expensive() -> str: ...
```

Without writing any spec code, every operation that uses this middleware
gets:
- the `X-Client-Id` header in its `parameters`,
- `429 Too Many Requests` in its `responses` (with the body schema for `str`).

The return-type union *is* the OpenAPI contract. Bare `OpResult` in the
union is the *passthrough* sentinel and contributes nothing; subclasses
contribute their response code. Middlewares must return an `OpResult`
(typically by calling `call_next(ctx)`); anything else raises `TypeError`
at request time.

> **SSE caveat.** When the wrapped operation streams an SSE response,
> `call_next` returns an `EventStreamResult` whose body is an iterator. A
> middleware can wrap that iterator (transforming/dropping events) but
> can't post-process headers after streaming has started — they're sent
> before the first event.

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

### Built-in auth middlewares

```python
from localpost.openapi import HttpApp, HttpBearerAuth, HttpBasicAuth

def validate_token(token: str) -> dict | None:
    # Return any truthy principal on success, None on failure.
    # The principal is stashed on ctx.attrs[<middleware>] for handlers to read.
    return decode_jwt(token)


app = HttpApp(middlewares=[HttpBearerAuth(validator=validate_token)])


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

## Async flavour (`HttpAsyncApp`)

Parallel to `HttpApp`, like `httpx.Client` and `httpx.AsyncClient`.
Same decorator API, same OpenAPI 3.2 emission, same `OpResult`
hierarchy — handlers are `async def`, middleware is built with
`@async_op_middleware`, and the deployment target is **ASGI** (uvicorn,
hypercorn, or `granian --interface asgi`):

```python
import sys
from dataclasses import dataclass

import uvicorn

from localpost.openapi import HttpAsyncApp, NotFound


@dataclass
class Book:
    id: str
    title: str


app = HttpAsyncApp()


@app.get("/books/{book_id}")
async def get_book(book_id: str) -> Book | NotFound[str]:
    book = await fetch_book(book_id)   # your async DB call, etc.
    if book is None:
        return NotFound(f"Book not found: {book_id}")
    return book


if __name__ == "__main__":
    sys.exit(uvicorn.run(app.asgi(), host="127.0.0.1", port=8000))
```

For `localpost.hosting` integration use
`app.service(uvicorn.Config(...))` and feed it to
`hosting.run_app(...)` — same shape as `HttpApp.service(config)` but
running the ASGI app under uvicorn.

A side-by-side example ports the sync `examples/openapi/app.py`
verbatim: see [`examples/openapi/async_app.py`](https://github.com/alexeyshockov/localpost.py/blob/main/examples/openapi/async_app.py).

### What's different

- **Handlers are async.** Plain `async def` for normal routes;
  `async def` generators for SSE. Sync generators are rejected at
  request time — `HttpAsyncApp` won't silently bridge a blocking
  generator onto the event loop.
- **Middleware is async.** Use `@async_op_middleware` to build them.
  Sync `OpMiddleware` instances are rejected at app construction time:

  ```python
  from typing import Annotated

  from localpost.openapi import (
      AsyncApiOperation, AsyncHTTPReqCtx, FromHeader,
      OpResult, Unauthorized, async_op_middleware,
  )


  @async_op_middleware
  async def require_api_key(
      ctx: AsyncHTTPReqCtx,
      call_next: AsyncApiOperation,
      x_api_key: Annotated[str, FromHeader("X-API-Key")] = "",
  ) -> Unauthorized[str] | OpResult:
      if x_api_key != "secret":
          return Unauthorized("Invalid or missing X-API-Key")
      return await call_next(ctx)
  ```
- **Auth.** `AsyncHttpBearerAuth` and `AsyncHttpBasicAuth` mirror their
  sync siblings; the validator may be sync or async.
- **Body buffering.** The ASGI bridge pre-buffers the request body
  before dispatch (matches the JSON-API common case the sync flavour
  is tuned for) so the same `FromBody` resolver works in both
  flavours. Cap the size with `HttpAsyncApp(max_body_size=N)` (default
  1 MiB; `-1` disables). Streaming uploads via `ctx.receive(...)` are
  on the follow-up list.
- **Resolvers.** `FromPath`, `FromQuery`, `FromHeader`, `FromBody` are
  shared — they only read sync attributes (`request`, `body`, `attrs`)
  off the ctx, so the same factories work in both apps.

### Deployment

Two flavours, depending on which target you ship to.

**ASGI** — `app.asgi()` returns a plain ASGI 3 callable; any ASGI
server runs it:

```python
# myapp.py
from localpost.openapi import HttpAsyncApp

app = HttpAsyncApp()
# … register routes …
asgi_app = app.asgi()
```

```bash
uvicorn myapp:asgi_app
hypercorn myapp:asgi_app
granian --interface asgi myapp:asgi_app
```

**RSGI (Granian native)** — `app.as_rsgi()` returns an RSGI application:

```python
rsgi_app = app.as_rsgi()
```

```bash
granian --interface rsgi myapp:rsgi_app
```

The RSGI bridge (`localpost.http.to_rsgi`) is wire-format-only — the
same handler chain serves both ASGI and RSGI traffic. Granian's RSGI
gives a single eager `response_bytes` per response (vs ASGI's
two-event start+body), zero-copy `sendfile` via
`response_file_range`, and direct `async for` body reads in streaming
mode. Requires the `[rsgi]` extra (`pip install 'localpost[rsgi]'`).

**Hosted apps under Granian** — when the HTTP app shares its worker
process with other hosted services (scheduler, gRPC, custom workers),
deploy through `localpost.hosting.rsgi.HostRSGIApp` instead, which runs the
full hosting lifecycle inside each Granian worker:

```python
from localpost.hosting.rsgi import HostRSGIApp
from localpost.scheduler import every, scheduled_task

@scheduled_task(every(seconds=5))
async def heartbeat(): ...


rsgi_app = HostRSGIApp(
    services=[heartbeat.service()],
    rsgi_handler=app,
)

# granian --interface rsgi --workers 4 myapp:rsgi_app
```

See [hosting](hosting.md#host-as-rsgi-for-granian) for the topology details.

## Design notes

See the in-tree design doc for rationale and a layout map: keep this README
focused on user-facing concepts.

## Sub-modules

| Module | Provides |
|---|---|
| `app.py` | `HttpApp`, registration, hosting entrypoint, built-in `/openapi.json` + `/docs` UIs (sync) |
| `operation.py` | `Operation`: sync runtime closure |
| `_operation_core.py` | Shared type-level core (signature parsing, return-type → response-shape, doc helpers, response encoding) — used by both flavours |
| `resolvers.py` | `FromPath`, `FromQuery`, `FromHeader`, `FromBody` factories — shared between sync and async |
| `results.py` | `OpResult` hierarchy (incl. `EventStreamResult`) |
| `middleware.py` | `OpMiddleware` protocol, `@op_middleware`, `ApiOperation` (sync) |
| `auth.py` | `HttpBearerAuth`, `HttpBasicAuth` — concrete auth middlewares (sync) |
| `aio/` | Async flavour: `HttpAsyncApp`, `AsyncOperation`, `AsyncOpMiddleware`, `@async_op_middleware`, `AsyncHttpBearerAuth`, `AsyncHttpBasicAuth`, `AsyncHTTPReqCtx`, ASGI bridge |
| `sse.py` | `Event`, `EventStream`, encoder — Server-Sent Events (sync drive) |
| `spec.py` | OpenAPI 3.2 dataclasses |
| `schemas.py` | `SchemaRegistry` — msgspec / pydantic JSON Schema generation |
| `pydantic.py` | Explicit pydantic helpers (auto-detection in `FromBody` works without this) |
| `_docs.py` | HTML for Swagger UI / ReDoc / Scalar (CDN-loaded) |
