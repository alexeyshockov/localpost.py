# localpost.http

> **Status:** stable — public API is not expected to break in patch/minor releases.

A small synchronous HTTP/1.1 server built on [h11](https://h11.readthedocs.io/),
plus a router for URI-template-based request dispatch, and a WSGI bridge. About
400 lines of focused, sync code — easy to read, easy to embed.

Pair it with `localpost.hosting` for lifecycle management, or run it standalone.
For OpenAPI / content negotiation / validation, see
[`localpost.openapi`](../openapi/README.md).

## Install

```bash
pip install localpost[http-server]
```

## Quick start

```python
import h11
from localpost.http.config import ServerConfig
from localpost.http.server import HTTPReqCtx, start_http_server


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
from localpost.hosting import run_app
from localpost.http._service import http_server
from localpost.http.config import ServerConfig

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

## Public API

### `localpost.http.server`

| Symbol                    | Notes                                      |
| ------------------------- | ------------------------------------------ |
| `start_http_server(cfg, handler)` | Context manager yielding a `Server` bound to ``handler`` |
| `HTTPReqCtx`              | Per-request context (`headers`, `body`, `complete`) |
| `RequestHandler`          | `Callable[[HTTPReqCtx], None]`             |

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

### `localpost.http.flask`

Native Flask adapter — optional extra `[http-flask]`. Bypasses WSGI on both
sides: drives Flask's pipeline directly and streams the Werkzeug `Response`
straight to h11. See [`flask.py`](flask.py).

| Symbol                                    | Notes                                                               |
| ----------------------------------------- | ------------------------------------------------------------------- |
| `flask_handler(app)`                      | Flask → `RequestHandler`                                            |
| `flask_server(config, app, *, max_concurrency=1)` | Hosted service serving a Flask app                          |

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

**Behavior differences from `wsgi_server`** (with a Flask app):

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

### Hosting integration — `localpost.http._service`

`@hosting.service` wrappers for running the server inside a hosted app:

| Service                                      | Notes                                       |
| -------------------------------------------- | ------------------------------------------- |
| `http_server(config, handler)`               | Runs the server loop with your handler      |
| `wsgi_server(config, app)`                   | Same, for a generic WSGI app                |
| `flask_server(config, app)`                  | Native Flask — see `localpost.http.flask`   |

The server loop runs in a worker thread (`anyio.to_thread.run_sync`); shutdown
is driven by `sl.shutting_down` via `threadtools.check_cancelled()`.

## How is it different from…

- **Flask** — Flask is web-first (templates, Jinja, sessions) with no built-in
  OpenAPI support. `localpost.http` is a low-level server; pair it with
  `localpost.openapi` for type-driven OpenAPI.
- **FastAPI** — FastAPI is async, Pydantic-only, OpenAPI-only, and ships a
  dependency-injection system. `localpost.http` is sync, has no opinions on
  serialization, and is small enough to read in one sitting.

## See also

- Examples: [`examples/http/`](../../examples/http/)
- OpenAPI on top: [`../openapi/README.md`](../openapi/README.md)
- Server source: [`server.py`](server.py)
- Router source: [`router.py`](router.py)
