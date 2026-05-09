# localpost.http

A small synchronous HTTP/1.1 server built on
[h11](https://h11.readthedocs.io/), plus a URI-template router, WSGI / ASGI
bridges, and a small framework (`HttpApp`) on top. Three layers, each
usable on its own:

- **Server**: `start_http_server` accepts connections, parses HTTP
  (h11 by default; httptools opt-in via `ServerConfig.backend`),
  dispatches to a `RequestHandler`. ~540 lines of sync code.
- **Router**: thin URI-template dispatcher. Matches the request,
  attaches a `RouteMatch` to `ctx.attrs[RouteMatch]`, delegates to
  the registered handler. 404 / 405 inline.
- **HttpApp**: decorator-driven framework — parameter injection,
  response conversion, worker-pool dispatch, middleware.

Pair with `localpost.hosting` for lifecycle management, or run any of
the three layers standalone.

## Scope

In-process only (no `fork` / `spawn`); for multi-core fanout, run multiple
processes under an external supervisor. Sync server only — async handlers
are reached via `localpost.http.asgi.to_asgi(handler)` plugged into
uvicorn / hypercorn / granian. CPython 3.12+ is the baseline; free-threaded
builds are an accepted target.

The hot path is tuned for the JSON-API common case: handlers can reject
before the body (no worker hop, `Content-Length` pre-checked against
`max_body_size`); read the body explicitly via `read_body(ctx)` /
`aread_body(ctx)` when needed; one-shot `complete()` flushes
status+headers+body in a single `sendall`. No HTTP/1.1 pipelining.

## Install

```bash
pip install localpost[http-server]            # h11 backend (default, pure Python)
pip install localpost[http-server,http-fast]  # also adds the httptools backend
```

## Quick start

The recommended path is `HttpApp`:

```python
from localpost.hosting import run_app
from localpost.http import HTTPReqCtx, ServerConfig
from localpost.http.app import HttpApp


app = HttpApp()


@app.get("/{name}")
def hello(name: str):
    return f"Hello, {name}!"


@app.post("/{name}/profile")
def update_profile(ctx: HTTPReqCtx, name: str):
    import json
    from localpost.http import read_body
    profile = json.loads(read_body(ctx))
    return {"updated": name, "profile": profile}


run_app(app.service(ServerConfig(host="127.0.0.1", port=8000)))
```

Or stay close to the wire — `start_http_server` directly:

```python
import h11
from localpost.http import HTTPReqCtx, ServerConfig, start_http_server


def simple_app(ctx: HTTPReqCtx):
    ctx.complete(
        h11.Response(status_code=200, headers=[(b"Content-Type", b"text/plain")]),
        b"Hello, World!\n",
    )


with start_http_server(ServerConfig(), simple_app) as server:
    while True:
        server.run()
```

Running under hosting:

```python
from localpost.hosting import run_app
from localpost.http import http_server, ServerConfig

# `simple_app` from the Quick start above
run_app(http_server(ServerConfig(), simple_app))
```

See [`examples/http/`](https://github.com/alexeyshockov/localpost.py/tree/main/examples/http/).

## Key concepts

- **`ServerConfig`** — host, port, backlog, `select_timeout`, `rw_timeout`,
  `keep_alive_timeout`, `max_body_size`.
- **`start_http_server(config, handler)`** — context manager; yields a
  `Server` bound to a non-blocking listening socket with a `selectors`
  poller. The handler is fixed for the server's lifetime.
- **`HTTPReqCtx`** — per-request context carrying the parsed h11 request,
  the raw socket, headers, and `complete(response, body)`. Request bodies
  are streamed via `receive(n_bytes)`.
- **`RequestHandler = Callable[[HTTPReqCtx], None]`** — the handler
  interface. `Server.run()` dispatches each accepted request to it.
- **`URITemplate`** — RFC 6570 Level 1 only (`/books/{id}` style
  variables). `match(uri) → dict | None`.
- **`Routes`** — mutable builder. Accumulate routes via decorators
  (`@routes.get("/path")`, `.post`, `.put`, `.delete`, `.patch`, `.add`),
  then call `.build()` to compile into a `Router`.
- **`Router`** — immutable, compiled URI-template dispatcher. One regex
  alternation over all templates, templates ordered by longest literal
  prefix, `Allow` headers pre-rendered. Exposes `.as_handler()` (native
  `RequestHandler`) and `.wsgi` (for deployment under Gunicorn / Granian
  / etc.). Build via `routes.build()` or `Router.from_routes(routes)`.

## Sub-modules

User-facing prose for the bridges and middlewares. The connection
model, threading topologies, and backend rationale live in `docs/design/`
— see the [Design](#design) section.

### `localpost.http.wsgi`

`wrap_wsgi(app)` turns a WSGI app into a `RequestHandler`;
`to_wsgi(handler)` turns a `RequestHandler` into a WSGI app.

### `localpost.http.asgi`

The async sibling of `localpost.http.wsgi` — bridges between a foreign
async protocol (ASGI 3) and the framework's async request-context shape
(`AsyncHTTPReqCtx`, defined in `localpost.http`). `localpost.http`
itself doesn't ship an async server; this module plugs an
`AsyncRequestHandler` into uvicorn / hypercorn / granian / any ASGI 3
server.

`to_asgi(handler, *, max_body_size=1<<20)` wraps an `AsyncRequestHandler`
as an ASGI 3 app. Body bytes are pulled lazily via
`await ctx.receive(size)` (or `aread_body(ctx)` for the whole-body common
case); `Content-Length` is pre-checked against `max_body_size` when
present (413 before dispatch).

```python
# myapp.py
from localpost.http import Response
from localpost.http.asgi import to_asgi


async def hello(ctx):
    await ctx.complete(Response(200), b"hi")


asgi_app = to_asgi(hello)
```

```bash
uvicorn myapp:asgi_app
hypercorn myapp:asgi_app
granian --interface asgi myapp:asgi_app
```

For the framework-flavoured surface (decorators, OpenAPI generation,
typed handlers) on top of the same bridge, see
[`localpost.openapi.HttpAsyncApp`](openapi.md#async-flavour-httpasyncapp)
— `app.asgi()` is sugar over `to_asgi(self._build_async_handler())`.

Cancellation: `ctx.disconnected` flips on `http.disconnect`. SSE
generators / long handlers poll it between events to short-circuit
cleanly. Body-handling contract across transports lives in
[request body handling](../design/request-body-handling.md).

### `localpost.http.rsgi`

The Granian-flavoured sibling of `localpost.http.asgi`. Same
`AsyncHTTPReqCtx` Protocol; different wire surface (RSGI exposes
richer per-method calls than ASGI's two-event response dance), and
different deployment topology (Granian is a process supervisor, not
an in-process server). Install: `pip install 'localpost[rsgi]'`.

`to_rsgi(handler, *, max_body_size=1<<20)` wraps an
`AsyncRequestHandler` for `granian --interface rsgi`. Single eager
`proto.response_bytes` per `complete`; zero-copy
`proto.response_file_range` for `sendfile` when the file has a path;
chunked stream fallback otherwise.

```python
# myapp.py
from localpost.http import Response
from localpost.http.rsgi import to_rsgi


async def hello(ctx):
    await ctx.complete(Response(200), b"hi")


rsgi_app = to_rsgi(hello)
```

```bash
granian --interface rsgi myapp:rsgi_app
```

For framework-flavoured deployment, see
[`localpost.openapi.HttpAsyncApp.as_rsgi()`](openapi.md#async-flavour-httpasyncapp).
For deployments where the HTTP app shares a worker process with **other
hosted services** (scheduler / gRPC / custom workers),
[`localpost.hosting.rsgi.HostRSGIApp`](hosting.md#host-as-rsgi-for-granian)
runs the full hosting lifecycle inside each Granian worker. The
asymmetry between uvicorn-as-a-hosted-service and Granian-as-a-supervisor
is covered in
[deployment topologies](../design/deployment-topologies.md).

### `localpost.http.static`

`static_handler(root, *, prefix=b"/", cache_control=None,
index="index.html")` builds a `RequestHandler` that serves files under
`root` via `socket.sendfile()` — zero-copy from the page cache to the
socket. Designed for **CDN-fronted deployments**: pair with proper
`Cache-Control` headers and origin sees roughly one hit per file per
edge per cache lifetime, which is why we skip on-the-fly compression
(the CDN handles `gzip` / `br` at the edge).

Behaviour:

- **Methods**: `GET` and `HEAD`. Anything else returns 405 + `Allow: GET, HEAD`.
- **Resolution**: percent-decoded URL path (with `prefix` stripped) is
  joined under `root`, resolved, and checked with `Path.is_relative_to`.
  `..` segments are rejected before resolution.
- **Conditional GET**: strong `ETag` (size + mtime in nanoseconds) plus
  `Last-Modified`. `If-None-Match` (with weak comparison) and
  `If-Modified-Since` short-circuit to 304 inline on the selector — no
  worker hop, no file open.
- **Range**: single byte-range only (`bytes=N-M`, `bytes=N-`, `bytes=-K`).
  Multi-range / unparseable falls back to 200 (RFC 7233 §3.1 compliant).
  Out-of-bounds → 416 with `Content-Range: bytes */<size>`.
- **Body**: 200 / 206 GET opens the file and calls `ctx.sendfile(...)`
  — wrap in `thread_pool_handler` to dispatch the syscall to a worker.
  HEAD success / 304 / 416 / 404 / 405 complete inline on the selector.

A static handler in its own pool, separate from the API pool, so a few
slow downloads can't pin all the API workers:

```python
from localpost.http import (
    Routes, ServerConfig, http_server, static_handler, thread_pool_handler,
)

routes = Routes()
# ... register API routes ...

api    = thread_pool_handler(routes.build().as_handler())
static = thread_pool_handler(
    static_handler("/var/www", prefix=b"/static/",
                   cache_control="public, max-age=31536000, immutable"),
)

async with api as api_h, static as static_h:
    def root(ctx):
        return (static_h if ctx.request.path.startswith(b"/static/") else api_h)(ctx)
    async with http_server(ServerConfig(), root):
        ...
```

Both wrappers share the process-wide worker pool — workers are spawned
on demand and reused. See
[`examples/http/static_files.py`](https://github.com/alexeyshockov/localpost.py/blob/main/examples/http/static_files.py).

`ctx.sendfile(response, file, offset, count)` is part of the public
`HTTPReqCtx` Protocol and can be used directly for any zero-copy body.
Requires `Content-Length: <count>` on the response (chunked is rejected);
both backends keep their parser state consistent with what the kernel
writes out-of-band.

### `localpost.http.compress`

`compress_handler(inner, *, algorithms=("br","gzip"), min_size=1024,
compressible_types=...)` wraps a `RequestHandler` so eligible
`complete()` responses are compressed — `gzip` (stdlib, always
available) plus `br` (optional via `[http-compress]`). Pair with a JSON
/ HTML / XML API; **not** intended for static files (compression and
zero-copy `sendfile` are at odds).

Behind a CDN you usually don't need this: the CDN compresses at the
edge from an uncompressed origin. `compress_handler` is for deployments
*not* behind a CDN, or when the CDN doesn't compress (rare).

The middleware skips compression when any of these hold (response sent
verbatim):

- `Accept-Encoding` doesn't list any configured `algorithms` with q>0
- Method is `HEAD` (no body to compress)
- `body is None` or `len(body) < min_size`
- Status is `1xx` / `204` / `304` (no body) or `206` (range — compressing
  breaks byte semantics)
- Response already has `Content-Encoding` (other than `identity`)
- Response has `Cache-Control: no-transform` (RFC 9111)
- `Content-Type` main-type is not in `compressible_types`

When eligible: body is compressed, `Content-Length` is replaced,
`Content-Encoding` is added, and `Accept-Encoding` is merged into
`Vary` (existing `Vary: Cookie` becomes `Vary: Cookie, Accept-Encoding`;
`Vary: *` is left alone).

`compress_handler` intercepts both `complete(...)` and `stream(...)`.
The middleware decides per-request:

- One-shot path → compress the whole body in memory; replace
  `Content-Length`.
- Streaming path with **no** `Content-Length` declared → wrap the chunk
  iterator with an incremental compressor; each input chunk is emitted
  compressed + sync-flushed so the decompressor sees each chunk
  promptly. The backend auto-frames `Transfer-Encoding: chunked` on
  HTTP/1.1.
- Streaming path **with** `Content-Length` → pass through.

For SSE (`Content-Type: text/event-stream`, in
`DEFAULT_COMPRESSIBLE_TYPES`), each event your generator yields reaches
the client decompressed and parseable by `EventSource` — same approach
nginx uses with `gzip on; gzip_types text/event-stream`. `sendfile`
always passes through uncompressed — composition with the static
handler stays zero-copy.

Limitations: the one-shot path allocates a compressed buffer per
response (fine for typical JSON; for multi-MB single-shot payloads,
hand `compress_handler` a `stream(response, chunks)` instead). Brotli
is opt-in (`pip install localpost[http-compress]`); if `"br"` is in
`algorithms` without the extra, `compress_handler` raises
`ImportError` at construction time. See
[`examples/http/compressed_api.py`](https://github.com/alexeyshockov/localpost.py/blob/main/examples/http/compressed_api.py).

### `localpost.http.flask`

Native Flask adapter — optional extra `[http-flask]`. `flask_handler(app)`
turns a Flask app into a `RequestHandler`; `flask_server(config, app)`
hosts it as a service (selector-thread, no pool). Bypasses WSGI on both
sides: drives Flask's pipeline directly and streams the Werkzeug
`Response` straight to h11.

### `localpost.http.router_sentry` / `localpost.http.flask_sentry`

Sentry tracing wrappers. `sentry_router_handler(router, *, op=...)`
wraps a `Router` in a Sentry transaction per request — transaction
named `"METHOD /books/{id}"` (the URI template, low cardinality) on a
match, or `"METHOD /raw/path"` on a miss. Optional extra
`[http-sentry]`.

`sentry_flask_handler(app, *, op=...)` wraps the native Flask adapter.
Requires both `[http-flask]` and `[http-sentry]`. Sentry's stock
`FlaskIntegration` ends the transaction when the WSGI `wsgi_app`
returns — *before* the body is iterated. Spans / errors inside a
streaming generator land outside the request transaction (or are
dropped). Because our Flask adapter holds the request context (and the
transaction) open through `response.iter_encoded()`, this fix-pack
version keeps everything on the same transaction. Behaviour
differences from `wsgi_server`: Flask's request context is active
during response-body iteration (a generator returned from a view can
use `flask.request`, `session`, `g` without `@stream_with_context`);
`teardown_request` / `teardown_appcontext` run **after** the body is
fully sent. Adapter touches Werkzeug/Flask internals (stable across
Flask 3.x but not a long-term contract) — use `wsgi_server` for
framework-agnostic WSGI.

### Hosting integration

`http_server(config, handler, *, selectors=1, acceptor=False)` is the
`@hosting.service`-decorated wrapper. `wsgi_server(config, app, ...)`
is the same shape for a generic WSGI app. `flask_server(config, app)`
covers the native Flask adapter. `thread_pool_handler(inner)` is an
async CM that yields a `RequestHandler` running `inner` on a shared
worker thread (process-wide pool, workers spawned on demand, no
concurrency cap).

Threading topology (`selectors=N`, `acceptor=True`) is covered in
[threading topologies](../design/threading-topologies.md).

## Sync vs. async surface

`HTTPReqCtx` (sync) and `AsyncHTTPReqCtx` (async) share the data side
and the terminal write methods. A handler that touches only the core
surface is portable between transports. The full asymmetry table and
why each member differs lives in
[connection model](../design/connection-model.md#sync-vs-async-request-context-surface).

## Cancellation

`HTTPReqCtx.disconnected` is a pull-style poll for peer-gone, mirroring
`AsyncHTTPReqCtx.disconnected`. Native backends do a non-blocking
`recv(1, MSG_PEEK | MSG_DONTWAIT)` on the request socket and stick `True`
once seen; the WSGI bridge always returns `False` (no socket handle).

For sync handlers without `ctx` in scope, `check_cancelled()` raises
`RequestCancelled` if the client disconnected (detected via
non-blocking `MSG_PEEK`) or the hosted service is shutting down. Call
periodically in long-running handlers.

## Design

The design rationale lives in `docs/design/`:

- [Connection model](../design/connection-model.md) — dispatch chain,
  two-state TRACKED/BORROWED machine, pull-based disconnect detection,
  sync-vs-async asymmetry.
- [Threading topologies](../design/threading-topologies.md) —
  `selectors=N` / acceptor topology, composition pattern, three orthogonal
  concerns (handler / router / `http_server`).
- [Server backends](../design/server-backends.md) — h11 and httptools
  coexistence, why no unified parser Protocol, httptools caveats.
- [Request body handling](../design/request-body-handling.md) — the
  `ctx.receive(size)` contract across native sync, WSGI, ASGI, and RSGI.
- [Deployment topologies](../design/deployment-topologies.md) — hosted
  services *inside* `run_app` (uvicorn / hypercorn) vs Granian as a
  process supervisor that runs the host *inside* its workers.

The native server is sync-only by design — async transports plug in via
`localpost.http.asgi.to_asgi` rather than growing a parallel async
request path inside this module.

## See also

- Examples: [`examples/http/`](https://github.com/alexeyshockov/localpost.py/tree/main/examples/http/)
- Server source: [`server_h11.py`](https://github.com/alexeyshockov/localpost.py/blob/main/localpost/http/server_h11.py)
- Router source: [`router.py`](https://github.com/alexeyshockov/localpost.py/blob/main/localpost/http/router.py)
