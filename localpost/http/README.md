# localpost.http

> **Status:** stable — public API is not expected to break in patch/minor releases.

A small synchronous HTTP/1.1 server built on [h11](https://h11.readthedocs.io/),
plus a URI-template router, a WSGI bridge, and a small framework
(`HttpApp`) on top. Three layers, each usable on its own:

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

## Scope and constraints

The server is intentionally bounded:

- **In-process only.** No multi-processing, no `fork` / `spawn`. If you need
  multi-core fanout, run multiple `localpost` processes under an external
  supervisor (systemd, gunicorn, k8s replicas, etc.). Multi-*selector*
  inside one process is on the roadmap.
- **Sync handlers only.** No `asyncio` / `uvloop` / ASGI on the server side.
  The hosting layer's AnyIO integration is unaffected — that's lifecycle
  plumbing, not the request hot path. If you need an async server, use one
  of the ASGI servers via `localpost.hosting.services/`.
- **GIL or free-threaded.** Standard CPython 3.12+ is the baseline.
  Free-threaded builds (3.13t / 3.14t) are an accepted target — the design
  is single-writer-per-selector and uses only thread-safe primitives, but
  see `benchmarks/http/PERF_FINDINGS.md` for any noted caveats per release.

### Workload shape (the JSON-API common case)

The hot path is tuned for the JSON web-API common case, and we cut
corners on shapes that don't fit it:

- **Reject before body.** Handlers that decide based on headers
  (no-route, wrong method, auth) return `None` and the body is never
  recv'd. No worker hop, no allocation.
- **Body is buffered, not streamed.** When a handler needs the body
  (e.g. to deserialise JSON), it needs the *whole* body. The selector
  buffers it into `ctx.body` and invokes a `BodyHandler` continuation;
  the continuation just reads `ctx.body`. There is still a
  `ctx.receive(size)` streaming API but it isn't the optimised path.
- **Response is one chunk or SSE.** Most responses are one
  status+headers+body block; SSE generators emit the same opening
  block then per-event chunks. The response writer auto-buffers
  headers and flushes them with the first body chunk in a single
  `sendall` — common-case `complete(...)` is one syscall.
- **No HTTP/1.1 pipelining.** Pipelined clients are served sequentially
  (correct, just no parallelism). The simpler per-conn state machine
  is the trade we made for it.

## Install

```bash
pip install localpost[http-server]            # h11 backend (default, pure Python)
pip install localpost[http-server,http-fast]  # also adds the httptools backend
```

## Quick start

The recommended path is `HttpApp`:

```python
import sys
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
    profile = json.loads(ctx.body)
    return {"updated": name, "profile": profile}


sys.exit(run_app(app.service(ServerConfig(host="127.0.0.1", port=8000))))
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

See [`examples/http/simple_server.py`](../../examples/http/simple_server.py),
`multithread_server.py`, `wsgi_app_server.py`.

Running under hosting:

```python
import sys

from localpost.hosting import run_app
from localpost.http import http_server, ServerConfig

# `simple_app` from the Quick start above
sys.exit(run_app(http_server(ServerConfig(), simple_app)))
```

## Key concepts

- **`ServerConfig`** — host, port, backlog, `select_timeout`, `rw_timeout`,
  `keep_alive_timeout`, `max_body_size`.
- **`start_http_server(config, handler)`** — context manager; yields a `Server`
  bound to a non-blocking listening socket with a `selectors` poller. The
  handler is fixed for the server's lifetime.
- **`HTTPReqCtx`** — per-request context carrying the parsed h11 request, the
  raw socket, headers, and `complete(response, body)`. Request bodies are
  streamed via `receive(n_bytes)`.
- **`RequestHandler = Callable[[HTTPReqCtx], None]`** — the handler
  interface. `Server.run()` dispatches each accepted request to it.
- **`URITemplate`** — RFC 6570 Level 1 only (`/books/{id}` style variables,
  matched with a generated regex). `match(uri) → dict | None`.
- **`Routes`** — mutable builder. Accumulate routes via decorators
  (`@routes.get("/path")`, `.post`, `.put`, `.delete`, `.patch`, `.add`), then
  call `.build()` to compile into a `Router`.
- **`Router`** — immutable, compiled URI-template dispatcher. One regex
  alternation over all templates, templates ordered by longest literal prefix,
  `Allow` headers pre-rendered. Exposes `.as_handler()` (native
  `RequestHandler`) and `.wsgi` (for deployment under Gunicorn / Granian /
  etc.). Build via `routes.build()` or `Router.from_routes(routes)`.

### Dispatch chain

The internals are a four-link, loose-coupled chain:

```
Selector  ── owns fd→SelectorCallback map; nothing HTTP-specific
   │
   ▼
ConnHandler  ── after-accept policy; owns RequestHandler + ConnFactory;
   │              decides which Selector tracks the new conn
   ▼
RequestHandler  ── pre-body dispatch; returns BodyHandler|None
   │
   ▼
BodyHandler  ── post-body continuation
```

Each link knows only the next. `Selector` doesn't carry a `RequestHandler` —
the handler is owned by the `ConnHandler` and threaded into the conn at
construction. This factoring is what makes the acceptor topology (1 acceptor
thread + N worker selectors) drop in without touching the request hot path.

- **`Selector`** — dumb fd→callback dispatcher. Built-in callbacks:
  `_DrainWakeup` (wakeup pipe), `_AcceptListener` (listen socket), and
  `BaseHTTPConn` itself (a conn *is* its own per-fd callback).
- **`ConnHandler = Callable[[Selector, socket.socket, tuple[str, int]], None]`** —
  after-accept policy. Two built-ins: `TrackHere` (default — track on the
  selector that accepted) and `RoundRobinAcceptor` (acceptor topology —
  spread conns across worker selectors via `Selector.post_track`).
- All callbacks are spelled as **callable dataclasses** (not closures), so
  their state is `repr`-able for debugging.

## Public API

### Server core (`localpost.http`)

| Symbol                    | Notes                                      |
| ------------------------- | ------------------------------------------ |
| `start_http_server(cfg, handler)` | Context manager yielding a `Server` bound to ``handler`` |
| `HTTPReqCtx`              | Per-request context (`headers`, `body`, `complete`, `sendfile`) |
| `RequestHandler`          | `Callable[[HTTPReqCtx], None]`             |

### Selector / accept-side topology

| Symbol                    | Notes                                      |
| ------------------------- | ------------------------------------------ |
| `Selector`                | Dumb fd→callback dispatcher (op queue + wakeup pipe + stale-conn sweep). One per selector thread. |
| `SelectorCallback`        | `Callable[[Selector], None]` — registered per-fd, invoked when readable |
| `ConnHandler`             | `Callable[[Selector, sock, addr], None]` — after-accept policy |
| `ConnFactory`             | `Callable[[Selector, sock, addr, RequestHandler], BaseHTTPConn]` |
| `TrackHere`               | Default `ConnHandler`: build conn for accepting selector and `track()` it. Reproduces the all-in-one behaviour. |
| `RoundRobinAcceptor`      | `ConnHandler` for the acceptor topology — spreads new conns across a tuple of worker `Selector`s via `post_track` (cross-thread op queue). |

### Cancellation

| Symbol                | Notes                                                          |
| --------------------- | -------------------------------------------------------------- |
| `check_cancelled()`   | Cooperative cancel check for sync handlers. Raises `RequestCancelled` if the client disconnected (detected via non-blocking ``MSG_PEEK``) or the hosted service is shutting down. Call periodically in long-running handlers. |
| `RequestCancelled`    | Exception raised by `check_cancelled()`. Inherits from `Exception` (not `BaseException`) — catchable with `except Exception:`. |

### `localpost.http.router`

| Symbol                    | Notes                                      |
| ------------------------- | ------------------------------------------ |
| `URITemplate`             | Parse and match RFC 6570 L1 templates      |
| `RequestCtx`              | Routed request context (path args, query, body access) |
| `Routes`                  | Mutable builder — decorators (`.get`, `.post`, …) / `.add`. Call `.build()` to freeze |
| `Router`                  | Immutable, compiled dispatcher. `.as_handler()` for native, `.wsgi` for WSGI. `.routes` is a tuple of `Route`. |
| `Route`                   | One compiled route: `template`, `methods`, pre-rendered `allow_header` |
| `Response`                | Simple `(status, headers, body)` tuple     |

### `localpost.http.wsgi`

| Symbol                    | Notes                                      |
| ------------------------- | ------------------------------------------ |
| `wrap_wsgi(app)`          | Turn a WSGI app into a `RequestHandler`    |

### `localpost.http.static`

Static file serving via `socket.sendfile()` — zero-copy from the page
cache to the socket. Designed for **CDN-fronted deployments**: pair with
proper `Cache-Control` headers and origin sees roughly one hit per file
per edge per cache lifetime, which is why we skip on-the-fly compression
(the CDN handles `gzip` / `br` at the edge).

| Symbol                                                                              | Notes                                                                       |
| ----------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| `static_handler(root, *, prefix=b"/", cache_control=None, index="index.html")`      | Build a `RequestHandler` that serves files under ``root``.                  |

Behavior:

- **Methods**: `GET` and `HEAD`. Anything else returns 405 + `Allow: GET, HEAD`.
- **Resolution**: percent-decoded URL path (with ``prefix`` stripped) is
  joined under ``root``, resolved, and checked with `Path.is_relative_to`.
  ``..`` segments are rejected before resolution.
- **Conditional GET**: strong `ETag` (size + mtime in nanoseconds) plus
  `Last-Modified`. `If-None-Match` (with weak comparison) and
  `If-Modified-Since` short-circuit to 304 inline on the selector — no
  worker hop, no file open.
- **Range**: single byte-range only (`bytes=N-M`, `bytes=N-`, `bytes=-K`).
  Multi-range / unparseable falls back to 200 (RFC 7233 §3.1 compliant).
  Out-of-bounds → 416 with `Content-Range: bytes */<size>`.
- **Body**: 200 / 206 GET returns a `BodyHandler` that calls
  `ctx.sendfile(...)` — wrap in `thread_pool_handler` to dispatch the
  syscall to a worker. HEAD success / 304 / 416 / 404 / 405 complete
  inline on the selector.

#### Recommended composition

A static handler in its own pool, separate from the API pool, so a few
slow downloads can't pin all the API workers:

```python
from localpost.http import (
    Routes, ServerConfig, http_server, static_handler, thread_pool_handler,
)

routes = Routes()
# ... register API routes ...

api    = thread_pool_handler(routes.build().as_handler(), max_concurrency=8)
static = thread_pool_handler(
    static_handler("/var/www", prefix=b"/static/",
                   cache_control="public, max-age=31536000, immutable"),
    max_concurrency=128, backlog=64,
)

async with api as api_h, static as static_h:
    def root(ctx):
        return (static_h if ctx.request.path.startswith(b"/static/") else api_h)(ctx)
    async with http_server(ServerConfig(), root):
        ...
```

The API pool is small and CPU-shaped; the static pool is wide and I/O-shaped.
See [`examples/http/static_files.py`](../../examples/http/static_files.py).

#### `HTTPReqCtx.sendfile`

The static handler is the typical caller, but `ctx.sendfile(response,
file, offset, count)` is part of the public `HTTPReqCtx` Protocol and
can be used directly for any zero-copy body. Requires
`Content-Length: <count>` on the response (chunked is rejected); both
backends keep their parser state consistent with what the kernel writes
out-of-band.

### `localpost.http.compress`

Response compression middleware for the **dynamic** path —
`gzip` (stdlib, always available) + `br` (optional via `[http-compress]`).
Pair with a JSON / HTML / XML API; **not** intended for static files
(compression and zero-copy `sendfile` are at odds — see
`plans/compression-middleware.md`).

Behind a CDN you usually don't need this: the CDN compresses at the
edge from an uncompressed origin. `compress_handler` is for deployments
*not* behind a CDN, or when the CDN doesn't compress (rare).

| Symbol                                                                                          | Notes                                                                       |
| ----------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| `compress_handler(inner, *, algorithms=("br","gzip"), min_size=1024, compressible_types=...)`   | Wrap a `RequestHandler` so eligible `complete()` responses are compressed. |
| `DEFAULT_COMPRESSIBLE_TYPES`                                                                    | Frozenset allowlist (text/*, JSON, XML, JS, SVG, YAML, …) used by default.  |

#### Decision matrix

The middleware skips compression when any of these hold (response sent
verbatim):

- `Accept-Encoding` doesn't list any configured `algorithms` with q>0
- Method is `HEAD` (no body to compress)
- `body is None` or `len(body) < min_size`
- Status is `1xx` / `204` / `304` (no body) or `206` (range — compressing breaks byte semantics)
- Response already has `Content-Encoding` (other than `identity`)
- Response has `Cache-Control: no-transform` (RFC 9111)
- `Content-Type` main-type is not in `compressible_types`

When eligible: body is compressed, `Content-Length` is replaced,
`Content-Encoding` is added, and `Accept-Encoding` is merged into
`Vary` (existing `Vary: Cookie` becomes `Vary: Cookie, Accept-Encoding`;
`Vary: *` is left alone).

#### Streaming responses (incl. SSE)

`compress_handler` also intercepts the streaming-response path
(`start_response` → `send`* → `finish_response`). The middleware decides
per-request:

- One-shot path (`complete(response, body)`) → compress the whole body
  in memory; replace `Content-Length`.
- Streaming path with **no** `Content-Length` declared → use an
  incremental compressor; each `send(chunk)` emits the bytes followed
  by a sync-flush so the decompressor sees each chunk promptly. The
  backend auto-frames `Transfer-Encoding: chunked` on HTTP/1.1.
- Streaming path **with** `Content-Length` → pass through (we'd
  otherwise lie about the declared length).

For SSE (`Content-Type: text/event-stream`, included in
`DEFAULT_COMPRESSIBLE_TYPES`), each event you `send(...)` reaches the
client decompressed and parseable by `EventSource` — same approach
nginx uses with `gzip on; gzip_types text/event-stream`. All major
browsers transparently decompress `Content-Encoding: gzip` / `br`
streams before EventSource sees them.

`sendfile` always passes through uncompressed — composition with the
static handler stays zero-copy.

#### Limitations

- **One-shot path allocates a compressed buffer per response.** Fine for
  typical JSON; for multi-MB single-shot payloads consider building
  the response with `start_response` + `send` instead so the streaming
  compressor handles it incrementally.
- **Brotli is opt-in.** `pip install localpost[http-compress]`
  installs `brotli`. If `"br"` is in `algorithms` without the extra,
  `compress_handler` raises `ImportError` at construction time.

#### Composition with the static handler

`compress_handler` and `static_handler` compose cleanly — the
compression middleware passes `sendfile` through, so static stays
zero-copy:

```python
api    = thread_pool_handler(
    compress_handler(routes.build().as_handler(), algorithms=("br", "gzip")),
    max_concurrency=8,
)
static = thread_pool_handler(static_handler("/var/www", prefix=b"/static/"),
                             max_concurrency=128, backlog=64)
```

See [`examples/http/compressed_api.py`](../../examples/http/compressed_api.py).

### `localpost.http.flask`

Native Flask adapter — optional extra `[http-flask]`. Bypasses WSGI on both
sides: drives Flask's pipeline directly and streams the Werkzeug `Response`
straight to h11. See [`flask.py`](flask.py).

| Symbol                                    | Notes                                                               |
| ----------------------------------------- | ------------------------------------------------------------------- |
| `flask_handler(app)`                      | Flask → `RequestHandler`                                            |
| `flask_server(config, app)`               | Hosted service serving a Flask app (selector-thread, no pool)       |

### `localpost.http.router_sentry`

Sentry tracing wrapper for `Router`. Optional extra `[http-sentry]`. No Flask
dependency — use this with the native `Router` + `http_server` flow.

| Symbol                                          | Notes                                                                  |
| ----------------------------------------------- | ---------------------------------------------------------------------- |
| `sentry_router_handler(router, *, op="http.server")` | Wraps `router.as_handler()` in a Sentry transaction per request. |

Transaction is named `"METHOD /books/{id}"` (the URI template, low cardinality)
on a match, or `"METHOD /raw/path"` on a miss. `http.method`, `http.url`,
`http.response.status_code` are recorded. Spans started inside the handler
attach to the request transaction.

### `localpost.http.flask_sentry`

Sentry tracing wrapper for the native Flask adapter. Requires both
`[http-flask]` and `[http-sentry]`.

| Symbol                                          | Notes                                                                  |
| ----------------------------------------------- | ---------------------------------------------------------------------- |
| `sentry_flask_handler(app, *, op="http.server")` | Wraps Flask in a Sentry transaction that covers the entire request, **including response-body streaming**. |

Why this exists: Sentry's stock `FlaskIntegration` ends the transaction when
the WSGI `wsgi_app` returns — *before* the body is iterated. Spans / errors
inside a streaming generator land outside the request transaction (or are
dropped). Because our Flask adapter holds the request context (and the
transaction) open through `response.iter_encoded()`, this fix-pack version
keeps everything on the same transaction.

Transaction is named after Flask's `url_rule.rule` (e.g. `"GET /hello/<name>"`)
once routing has matched.

**Behavior differences from `wsgi_server`** (Flask served via `wrap_wsgi`):

- Flask's **request context is active during response-body iteration**. A
  generator returned from a view can use `flask.request`, `session`, `g`
  without `@stream_with_context`. (`stream_with_context` still works, just
  becomes a no-op.)
- `teardown_request` / `teardown_appcontext` run **after** the body is fully
  sent (the opposite of standard WSGI Flask). Resources like DB sessions
  stay alive for the duration of streaming.

Trade-off: the adapter touches Werkzeug/Flask internals (`app.request_context`,
`app.full_dispatch_request`, `Response.iter_encoded`). Stable across Flask 3.x
but not a long-term contract. Use `wsgi_server` for framework-agnostic WSGI
(Django, Flask without the extras, anything else) or when you want to stay
on the documented public Flask API.

### `localpost.http.config`

| Symbol        | Notes                                              |
| ------------- | -------------------------------------------------- |
| `ServerConfig` | Frozen dataclass of server tuning parameters      |
| `LOGGER_NAME` | `"localpost.http"`                                 |

### Hosting integration

| Symbol                                            | Module                       | Notes                                                                               |
| ------------------------------------------------- | ---------------------------- | ----------------------------------------------------------------------------------- |
| `http_server(config, handler, *, selectors=1, acceptor=False)` | `localpost.http._service`    | `@hosting.service` — runs the server loop with `handler`. See **Threading topologies** below. |
| `wsgi_server(config, app, *, selectors=1, acceptor=False)`     | `localpost.http._service`    | Same, for a generic WSGI app.                                                       |
| `flask_server(config, app)`                       | `localpost.http.flask`       | Native Flask — see `localpost.http.flask`.                                          |
| `thread_pool_handler(inner, *, max_concurrency, backlog=0)` | `localpost.http._pool` | Async CM. Yields a `RequestHandler` that runs `inner` on a worker thread. Admission cap = `max_concurrency + backlog`; default `backlog=0` means exactly `max_concurrency` in flight. |

#### Threading topologies

`http_server` supports three accept-side shapes, all sharing the same
`RequestHandler` / `BodyHandler` chain:

| Configuration                         | Threads                                       | When to use |
| ------------------------------------- | --------------------------------------------- | ----------- |
| Default (`selectors=1`)               | 1 thread accepts, parses, and dispatches      | Simple deployments; thread-pool wrapper handles concurrency on the request side |
| `selectors=N`                         | N independent selectors, each with its own listening socket via `SO_REUSEPORT` | Linux kernel-level connection load-balancing; free-threaded builds |
| `selectors=N, acceptor=True`          | 1 acceptor thread + N worker selector threads | macOS / free-threaded targets where `SO_REUSEPORT` doesn't distribute evenly. Acceptor accepts each conn and round-robins it to a worker via the cross-thread op queue. |

The server loop runs in a worker thread (`anyio.to_thread.run_sync`); shutdown
is driven by `lt.shutting_down` via `threadtools.check_cancelled()`. The
server hosts a single handler; whether that handler runs synchronously on the
selector thread or hops to a worker is the handler's choice.

#### Composition pattern

`http_server`, the `Router`, and `thread_pool_handler` are three orthogonal
concepts that you compose explicitly:

```python
from localpost.hosting import run_app, service
from localpost.http import (
    Routes, ServerConfig, http_server, thread_pool_handler,
)


@service
async def app():
    routes = Routes()

    @routes.get("/hello/{name}")
    def hello(ctx): ...   # plain RequestCtx → Response handler

    config = ServerConfig(host="127.0.0.1", port=8000)
    async with thread_pool_handler(routes.build().as_handler(), max_concurrency=8) as h:
        async with http_server(config, h):
            yield


run_app(app())
```

What this gives you:

- **404 / 405 stay on the selector thread.** When the `Router` is the handler
  (wrapped or not), unmatched paths and method mismatches go through
  `_send_plain` inline — no worker hop.
- **Matched routes run wherever you want.** Wrap the entire router with
  `thread_pool_handler` (above) to run all matched handlers on workers; pass
  the router directly to `http_server` to keep them all on the selector;
  more granular per-route control is the user's composition problem (today
  there is no per-route pool API).
- **No max\_concurrency on `http_server`.** The pool is the wrapper's
  concern; `http_server` has one job and one job only.
- **Admission is the pool's concern, not the server's.** The pool admits up to
  `max_concurrency + backlog` requests at once (a `threading.Semaphore`
  acquired by the selector on dispatch, released by the worker on completion).
  The default `backlog=0` is the strict-N case: every dispatch needs a free
  worker, otherwise it 503s. Bump `backlog` to let bursts queue briefly
  instead of bouncing.

## Design

### Sync handlers only — no async

`RequestHandler` is `Callable[[HTTPReqCtx], None]`, sync-only. **This is
intentional and not a planned extension.**

The whole package is built around blocking sockets: the selector accepts
connections, parses HTTP/1.1 with h11, and either dispatches the request
synchronously on the selector thread (e.g. for a 404 from `Router`) or
hands it off to a worker thread (when the user composes
`thread_pool_handler` into the handler chain).

If you need an async server, use one of the ASGI servers that already exist
(uvicorn, hypercorn, granian, …) — the `localpost.hosting` adapters in
`localpost.hosting.services/` plug them in cleanly. There is no need for
this package to grow a second, parallel async path.

### Three orthogonal concerns

- **Handler** — `Callable[[HTTPReqCtx], None]`. Immediate by default
  (runs on whichever thread invokes it). The thread-pool variant is just
  a wrapper that borrows the connection, queues it, and runs `inner` on
  a worker.
- **Router** — itself an immediate handler. Runs `_match()` inline on the
  calling thread; 404/405 are sent via `_send_plain` and the conn re-tracks
  via the existing connection loop. Matched routes call the registered
  per-route handler — which the user can choose to pool or not.
- **`http_server`** — hosting integration only. Owns the AnyIO bridge,
  drives `server.run()` in a thread, watches `lt.shutting_down`. Doesn't
  know about pools, doesn't know about routing.

This split means the simple case (all-immediate handlers) doesn't pay for a
worker pool, and the Router's 404/405 path doesn't either even when the
matched routes run on workers.

### Two-state connection model

A connection is either **TRACKED** (registered in the selector — selector
owns the fd, parser, and I/O) or **BORROWED** (a worker thread holds the
parser + socket; fd is unregistered). The dispatcher unregisters before
handing off to a worker (`stop_tracking`); `finish_response` re-registers
via `_maybe_give_back`. Two states, no third "watchdog" mode, no shared
mode field for threads to race on.

```
            accept()
                │
                ▼
       ConnHandler builds conn,
       routes to a selector via
         track() or post_track()
                │
                ▼
       ┌──────────────────────┐         ┌──────────┐
       │      TRACKED         │  close  │          │
       │  fd registered       ├────────▶│  CLOSED  │
       │  selector owns I/O   │         │          │
       └──┬───────────────────┘         └──────────┘
          │  ▲                                ▲
   borrow │  │ _maybe_give_back               │ close
          ▼  │                                │
       ┌──────────────────────┐               │
       │      BORROWED        │               │
       │  fd unregistered     ├───────────────┘
       │  worker owns I/O     │
       └──────────────────────┘
```

Both topologies (single selector and acceptor + N worker selectors)
funnel through the same diagram; only the entry edge differs:

| From → to                | Method               | Caller thread     | Mechanism                                        |
| ------------------------ | -------------------- | ----------------- | ------------------------------------------------ |
| accept → TRACKED         | `Selector.track`     | selector          | inline `selectors.register` (TrackHere topology) |
| accept → TRACKED         | `Selector.post_track`| acceptor          | op queue + wakeup pipe (RoundRobinAcceptor)      |
| TRACKED → BORROWED       | `stop_tracking`      | selector          | inline `selectors.unregister`                    |
| BORROWED → TRACKED       | `Selector.track`     | worker            | op queue + wakeup pipe                           |
| TRACKED → CLOSED         | `BaseHTTPConn.close` | selector          | inline (idle / keep-alive / error)               |
| BORROWED → CLOSED        | `close` + `post_close` | worker          | op queue + wakeup pipe (`_fd_to_key` cleanup)    |

The op queue + wakeup pipe is the only cross-thread synchronisation edge
(the `os.write` to the wakeup pipe is a full memory barrier). After
`track()` returns, anything the worker did to the conn's parser
(`h11.Connection.send` / `httptools.HttpRequestParser.feed_data`) is
visible to the selector. After `stop_tracking()` returns, anything the
selector did is visible to the worker. The parser is never touched
concurrently from two threads.

### Pull-based client-disconnect detection

While the worker holds a borrowed connection, client disconnects are
detected on demand: `check_cancelled()` does a non-blocking
`recv(1, MSG_PEEK | MSG_DONTWAIT)` on the request socket. `b""` means peer
FIN — `RequestCancelled` is raised. Handlers that do regular I/O surface
disconnects via `EPIPE` / `ECONNRESET` naturally; handlers that compute
without I/O should call `check_cancelled()` periodically (same contract as
service-shutdown cancellation).

This replaces an earlier push-based design where the selector kept the
socket registered in a third "watchdog" mode and fired EOF events. That
worked but introduced a 3-way state machine with cross-thread races. The
pull-based variant collapses to two states and one syscall per
`check_cancelled()` call.

## Server backends

Two parser implementations live side-by-side. They share the listening
socket, selector loop, op queue, stale-conn sweep, and shutdown
coordination (everything in `_base.py`). They differ only in how they
drive the parser:

| `ServerConfig.backend` | Parser      | Extra            | Notes                           |
| ---------------------- | ----------- | ---------------- | ------------------------------- |
| `"h11"` *(default)*    | h11         | `[http-server]`  | pure Python, readable           |
| `"httptools"`          | httptools   | `[http-fast]`    | C-based llhttp; faster header parsing |

There is one entry point — `start_http_server(config, handler)` — and
one hosted-service wrapper — `http_server(config, handler)`. The parser
is selected via `ServerConfig.backend`:

```python
from localpost.http import ServerConfig, start_http_server

with start_http_server(
    ServerConfig(backend="httptools"), my_handler
) as server:
    while True:
        server.run()
```

Pick whichever fits — handler code is identical. Both populate the same
neutral `Request` / `NativeResponse` types from `localpost.http`. The
two implementations are intentionally **not** unified behind a parser
Protocol: h11 is pull-events + parse/serialize, httptools is
push-callbacks + parse-only, and forcing one shape over both restricts
the faster backend without buying anything.

httptools backend caveats (initial scope):
- `Content-Length` response bodies only (chunked transfer-encoding is a
  follow-up). `Router` and `wrap_wsgi` already set `Content-Length`,
  so this matches today's behaviour.
- HTTP Upgrade negotiation surfaces as 400 Bad Request — revisit if/when
  WebSockets come back on the roadmap.

For perf context, see
[`benchmarks/http/PERF_FINDINGS.md`](../../benchmarks/http/PERF_FINDINGS.md).

## How is it different from…

- **Flask** — Flask is web-first (templates, Jinja, sessions). `localpost.http`
  is a low-level server with a small `HttpApp` framework on top — handy for
  JSON APIs, but no opinions on templating or serialization.
- **FastAPI** — FastAPI is async, Pydantic-only, OpenAPI-only, and ships a
  dependency-injection system. `localpost.http` is sync, has no opinions on
  serialization, and is small enough to read in one sitting.

## See also

- Examples: [`examples/http/`](../../examples/http/)
- Server source: [`server.py`](server.py)
- Router source: [`router.py`](router.py)
