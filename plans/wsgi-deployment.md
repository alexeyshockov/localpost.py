# WSGI deployment for `Router` and `HttpApp`

## Context

Today `Router` and `localpost.openapi.HttpApp` only run under
`localpost.http`'s native server (`http_server` / `start_http_server`).
The router and the OpenAPI framework only touch a small slice of
`HTTPReqCtx`, but the Protocol exposes localpost-server-only bits
(`selector`, `conn`, `borrow`) that prevent a WSGI implementation from
satisfying it.

Goal: deploy `HttpApp` (and bare `Router`) under Gunicorn / uWSGI /
Werkzeug. Single Protocol — no parallel `RequestCtx` — by trimming
`HTTPReqCtx` until both transports can satisfy it. ASGI is out of scope
for this plan.

## Surface audit

Used by router + `localpost.openapi`:
- `request` (`.path` / `.method` / `.query_string` / `.headers`)
- `body`, `attrs`, `response_status`
- `start_response` / `send` / `finish_response` / `complete`

Used by static handler:
- `sendfile(response, file, offset, count)`

Used by user-facing examples (`middleware_rate_limit.py`):
- `ctx.conn.sock.getpeername()[0]` — replace with `ctx.remote_addr`.

Used internally only (native server / `_pool.py` / `emit_handler_error`):
- `selector`, `conn`, `borrow`, `borrowed`.

`ctx.selector` is read in three places: `_pool.py` (concrete native; not
in Protocol after the trim), and `wsgi.py:_build_environ` for
``SERVER_NAME`` / ``SERVER_PORT``. A new `local_addr: str | None`
covers the latter without exposing the selector.

## Protocol changes

After the trim:

```python
class HTTPReqCtx(Protocol):
    request: Request
    body: bytes
    response_status: int | None
    attrs: dict[Any, Any]
    remote_addr: str | None
    local_addr: str | None
    scheme: str                # "http" | "https"
    borrowed: bool
    def borrow(self) -> AbstractContextManager[HTTPReqCtx]: ...
    def receive(self, size: int = ..., /) -> bytes: ...
    def start_response(self, r: Response | InformationalResponse, /) -> None: ...
    def send(self, chunk: Buffer, /) -> None: ...
    def finish_response(self) -> None: ...
    def complete(self, r: Response, body: bytes | None = None) -> None: ...
    def stream(self, r: Response, chunks: Iterator[bytes]) -> None: ...
    def sendfile(self, r: Response, file: BinaryIO, offset: int, count: int) -> None: ...
```

Removed from Protocol (kept on the concrete native ctx):
- `selector` — two callsites in `wsgi.py` (`_build_environ` for the
  inverse `wrap_wsgi` adapter); thread `(host, port)` through directly
  or reach the concrete ctx at those sites.
- `conn` — internal callers (`_pool.py`, `_base.emit_handler_error`,
  native backends) re-typed against the concrete native ctx; user-facing
  need (peer address) is covered by the new `remote_addr` field.

No-op on WSGI:
- `borrowed` — always `True` (the WSGI worker already owns the
  connection — that's exactly what borrowed means).
- `borrow()` — `nullcontext(self)`.

Added:
- `remote_addr: str | None` — natively from `socket.getpeername()`,
  under WSGI from `environ['REMOTE_ADDR']`.
- `local_addr: str | None` — natively from the listening socket
  (replaces the `ctx.selector.config.host` / `ctx.selector.port` reach),
  under WSGI from `environ['SERVER_NAME']` + `environ['SERVER_PORT']`.
- `scheme: str` — `"http"` or `"https"`. Natively defaults to `"http"`
  (no TLS today); under WSGI from `environ['wsgi.url_scheme']`. Useful
  for the OpenAPI ``servers`` list and redirect builders behind a
  TLS-terminating load balancer.
- `stream(response, chunks)` — declarative streaming (see below).

## SSE refactor — `ctx.stream(response, chunks)`

Today `_stream_sse` (`operation.py:576`) drives a manual
`start_response` / `send` loop with cancellation interleaving. This
becomes:

```python
def _stream_sse(ctx, result, adapters):
    headers = _merge_sse_headers(result.headers)
    ctx.stream(
        _Response(status_code=result.status_code, headers=headers),
        iter_events(result.body, adapters),
    )
```

Native impl wraps the existing imperative trio with cancellation:

```python
def stream(self, response, chunks):
    self.start_response(response)
    try:
        for chunk in chunks:
            try: check_cancelled()
            except LookupError: pass
            self.send(chunk)
    except RequestCancelled:
        return
    self.finish_response()
```

WSGI impl captures `(response, chunks)` and returns the iterator
straight to the WSGI server — zero threads, zero queue:

```python
def stream(self, response, chunks):
    self._streaming = (response, chunks)
```

The imperative trio (`start_response` / `send` / `finish_response`)
stays on the Protocol — `compress.py` still wraps it for incremental
compression, and hand-written streaming handlers keep working. WSGI
falls back to thread+queue for the imperative trio only.

## WSGI adapter

```python
def to_wsgi(handler: RequestHandler) -> WSGIApplication:
    def wsgi_app(environ, start_response):
        ctx = _WSGIReqCtx(environ)
        body_handler = handler(ctx)
        if body_handler is not None:
            body_handler(ctx)
        return ctx._respond(start_response)
    return wsgi_app
```

`_WSGIReqCtx._respond(start_response)` branches:
- eager `complete()` → `start_response(...)`; return `[body]`.
- `stream(...)` captured → `start_response(...)`; return `chunks`.
- imperative `start_response/send/.../finish_response` → thread+queue
  fallback.

`sendfile` — use `environ['wsgi.file_wrapper']` if present, else
chunked read+yield.

## Public API

```python
HttpApp.as_wsgi(self) -> WSGIApplication
Router.as_wsgi(self) -> WSGIApplication
```

Deployment: `gunicorn myapp:app.as_wsgi()`. `thread_pool_handler` does
not apply — the WSGI server's worker model is the pool.
`check_cancelled()` is a no-op under WSGI (no socket handle inside the
WSGI app); `_stream_sse` already gracefully handles `LookupError`.

## Footprint

- `_base.py` — slim Protocol; concrete native ctx adds `selector` /
  `conn` as instance attributes (not in Protocol). Add `stream()`
  default impl.
- `_pool.py`, `emit_handler_error` — re-typed to concrete native ctx
  (~5 lines).
- `wsgi.py` (existing inverse adapter) — drop `ctx.selector.config.host`
  reach; thread `(host, port)` into `_build_environ`.
- `examples/http/middleware_rate_limit.py` — `ctx.conn.sock.getpeername()`
  → `ctx.remote_addr`.
- New: `to_wsgi(handler)` + `_WSGIReqCtx` in `wsgi.py`.
- New: `HttpApp.as_wsgi()` / `Router.as_wsgi()`.
- `_stream_sse` rewritten to use `ctx.stream(...)`.

## Order of work

1. Slim Protocol (drop `selector` / `conn` / `borrow*`); add
   `remote_addr`. Re-type internal callers.
2. Add `ctx.stream()` to Protocol + native impl; rewrite `_stream_sse`.
3. `to_wsgi(handler)` + `_WSGIReqCtx` with eager / `stream` /
   imperative-fallback branches.
4. `HttpApp.as_wsgi()` / `Router.as_wsgi()` + example + tests.

## Forward compatibility — RSGI alignment

The shape after the trim is intentionally close to Granian's RSGI:
single ctx object, methods drive the response 0→100, handler keeps
control until exit. The mapping:

| `HTTPReqCtx`                          | RSGI                              |
| ------------------------------------- | --------------------------------- |
| `request.{path,method,headers,...}`   | `scope`                           |
| `request.http_version` (`b"1.1"` …)   | `scope.http_version`              |
| `remote_addr` / `local_addr`          | `scope.client` / `scope.server`   |
| `scheme`                              | `scope.scheme`                    |
| `body`, `receive(size)`               | `protocol.__call__()` / `__aiter__()` |
| `complete(response, body)`            | `response_bytes(...)`             |
| `stream(response, chunks)`            | `response_stream(...)` + `send_bytes(...)` |
| `sendfile(response, file, ...)`       | `response_file_range(...)`        |

This means an RSGI deployment adapter (`to_rsgi(handler)`) is a
straight protocol translation if/when we want it — no Protocol redesign.

### HTTP/2

Transparent. No Protocol changes — `request.http_version` already
takes `b"2"`. When we eventually want HTTP/2, swap the parser
(h2 / Hypercorn / Granian) — handler code is unchanged. `stream()` runs
over an HTTP/2 stream the same as it does over `Transfer-Encoding:
chunked`.

### WebSocket

Sibling Protocol, not a member of `HTTPReqCtx`. See
`plans/websocket-support.md` for the sketch. WSGI cannot carry WS;
`to_wsgi(handler)` raises if the registered routes include any WS
handlers, or just dispatches HTTP-only.

## Out of scope

- ASGI / RSGI deployment.
- HTTP/2 transport (Protocol is ready; needs a parser swap).
- WebSocket support (separate plan).
- Compression on the WSGI streaming path (works today via thread+queue
  fallback; revisit if needed).
