# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

### Added

- **Two-phase HTTP request handler contract.** `RequestHandler` is now
  `Callable[[HTTPReqCtx], BodyHandler | None]`. The pre-body handler runs
  on the selector thread when headers are parsed and may either complete
  inline (returning `None`, e.g. for 404 / 405 / auth fail) or return a
  `BodyHandler` continuation. The selector buffers the full request body
  into `ctx.body` before invoking the continuation. Old-style
  `(ctx) -> None` handlers are forward-compatible. See
  `benchmarks/http/PERF_FINDINGS.md` Phase 8 for the design rationale and
  bench numbers (+21% RPS on standard CPython 3.13 httptools).
- `localpost.http.BodyHandler` — type alias for the post-body continuation.
- **Free-threaded CPython (3.14t) support** — verified end-to-end with the
  full http test suite passing and the bench delivering a ~3x RPS jump at
  `selectors=1` (httptools plaintext: 12,563 → 36,208 RPS) just from
  switching interpreters. The existing single-selector + worker-pool
  architecture is already multi-threaded, so removing the GIL lets the
  selector and workers actually overlap. Tested with `httptools >= 0.8`
  (declares `Py_mod_gil = Py_MOD_GIL_NOT_USED`); the released 0.7.1 wheel
  auto-re-enables the GIL on import. See
  `benchmarks/http/PERF_FINDINGS.md` Phase 7b.
- **Multi-selector single-process** via the new `selectors: int = 1` knob
  on `localpost.http.http_server` and `httptools_server`. With
  `selectors > 1`, each selector thread binds its own listening socket on
  the same address via `SO_REUSEPORT`; the kernel distributes incoming
  connections across them. Handlers (and any wrapped
  `thread_pool_handler`) are shared. **Note: on macOS this knob is a
  no-op.** Verified empirically (`localpost_httptools_diag.py`): with
  `selectors=4`, 100% of accepts go to one selector thread. macOS's
  `SO_REUSEPORT` permits the bind but does not load-balance accepts —
  unlike Linux 3.9+. An accept-dispatch design (one acceptor thread +
  N selectors fed via the existing op queue) is the platform-portable
  fix and is the next planned step. The knob still works on Linux,
  pending a Linux bench cell to confirm scaling.
- `localpost.http.thread_pool_handler` — async context manager that wraps
  a `RequestHandler` so the post-body `BodyHandler` continuation it
  returns is offloaded to a worker thread. Pre-body decisions (routing,
  auth, 404 / 405) stay on the selector. Compose explicitly with
  `http_server` when you want body handlers on a worker pool.
- `just deadcode` — vulture-based dead-code finder, configured in
  `pyproject.toml` (`[tool.vulture]`).
- **Optional `httptools` HTTP server backend** under the `[http-fast]`
  extra. New entry points `localpost.http.start_httptools_server` and
  `localpost.http.httptools_server` (hosted-service form) drive a
  C-based llhttp parser as a peer of the existing h11 server. Same
  selector / accept loop / connection bookkeeping (lifted into a shared
  `BaseServer`); each backend uses its parser's natural idioms — no
  internal `next_event/send_response` Protocol forced over both.
  h11 stays the default. Initial scope: `Content-Length` responses
  only (chunked transfer-encoding on the httptools side is a follow-up;
  matches what `Router` and `wrap_wsgi` produce today).
- Neutral wire types `Request`, `NativeResponse`, `InformationalResponse`
  (re-exported from `localpost.http`) — backend-agnostic shapes both
  servers populate. User code no longer imports `h11` or `httptools`
  directly.

### Changed

- **`RequestHandler` return type changed** from `None` to
  `BodyHandler | None` (see Added). Old-style `(ctx) -> None` handlers
  still work — returning `None` is the inline-completion path.
  Handlers that previously read the body via `ctx.receive(size)` need
  to migrate to the continuation pattern: return a `BodyHandler` and
  read the body from `ctx.body`. The in-tree adapters
  (`Router.as_handler`, `wrap_wsgi`, `flask_handler`,
  `sentry_router_handler`, `sentry_flask_handler`) are updated.
- **HTTP/1.1 pipelining is no longer supported.** Pipelined clients are
  served sequentially — correct, but no parallelism on the same
  connection. The httptools backend's `_ready` deque was removed for the
  simplification.
- **`localpost.http` no longer leaks h11 types into the public API.**
  `HTTPReqCtx.request` is now `localpost.http.Request` (was
  `h11.Request`); `HTTPReqCtx.start_response` and `complete` accept
  `localpost.http.NativeResponse` / `InformationalResponse` (was
  `h11.Response` / `h11.InformationalResponse`). Field shapes are
  identical (lowercased header-name bytes, byte values), so the
  migration is mechanical: replace `import h11` /
  `h11.Response(status_code=…, headers=…)` with
  `from localpost.http import NativeResponse` /
  `NativeResponse(status_code=…, headers=…)`. The `http` module is
  marked stable, but absorbing this cost once enables the
  alternative-backend support above.
- `localpost.http._service.http_server` no longer accepts `max_concurrency`
  and no longer owns a worker pool. Wrap your handler with
  `thread_pool_handler` to opt back into worker dispatch.
- `localpost.http.flask.flask_server` and `localpost.http.wsgi_server`
  drop their `max_concurrency` kwarg for the same reason — wrap with
  `thread_pool_handler` if you need a pool (typical for blocking WSGI
  / Flask apps).
- **Experimental sub-packages moved.** `localpost.consumers` →
  `localpost.experimental.consumers`; `localpost.openapi` →
  `localpost.experimental.openapi`. The `experimental` segment in every
  import path is the new stability marker — README notes alone were too
  easy to miss. APIs themselves are unchanged.
- Modernised typing throughout `localpost/_utils.py`,
  `localpost/scheduler/`, and `localpost/hosting/_host.py` to PEP 695
  (`class Foo[T]`, `type Foo = ...`, inline function type parameters).
  No public-API change — the existing module-level `TypeVar` declarations
  in `localpost/scheduler/_scheduler.py` are kept until ty learns to
  reconcile PEP 695 class type parameters with same-named TypeVars
  inside nested generic functions.
- Dropped the `Programming Language :: Python :: 3.11` classifier
  (the project's `requires-python = ">=3.12"` since 0.6).
- Cleaner ruff/ty footprint across stable packages and shared infra
  (`localpost/__init__.py`, `_utils.py`, `threadtools.py`): 0 errors
  from either tool. Remaining warnings live entirely in
  `localpost/experimental/`.

### Removed

- Internal helpers that were unused everywhere: `localpost._utils.NO_OP_TS`,
  `AsyncContextManagerAdapter`, `Switch`, the `send_or_drop_from_thread` /
  `send_or_drop` methods on the now-trivial `MemorySendStream` (so
  ``MemoryStream.create()`` simply returns the bare anyio
  ``MemoryObjectSendStream`` / ``MemoryObjectReceiveStream`` pair), and
  `localpost.hosting._host._serve_and_observe`.

## [0.6.0] - 2026-02-22

Complete rewrite of the hosting system, to simplify it and make it more robust.

### Fixed

### Added

- `localpost.http` — selectors-based non-async HTTP server

### Changed

### Removed

- `localpost.flow` — too complicated

## [0.5.0] - 2025-07-18

### Added

- `localpost.consumers.stream` for in-memory queues
- `localpost.hosting.services.hypercorn` for Hypercorn HTTP server
- `localpost.debug` context manager, to simplify debugging
- More tests

### Changed

- `localpost.consumers.kafka` reworked
- `localpost.consumers.sqs` reworked (now with both `boto3` and `aioboto3` support)

## [0.4.0] - 2025-06-23

### Fixed

- `UvicornService` crashes the whole app if the server fails to start

### Added

- `HostedService` class, to represent a named hosted service
- Hosted service middlewares: start_timeout, shutdown_timeout, and lifespan
- Ability to combine multiple hosted services (`+` operator)
- Ability to wrap a hosted service (or a set of services) by another one (`>>` operator)
- `Host.state` (similar to `ServiceLifetime.state`)
- More tests

### Changed

- `EventView.__bool__()` in addition to `EventView.is_set()`
- `AppHost` reworked (simplified)
- `localpost.flow_ops` merged into `localpost.flow`

### Removed

- `localpost.scheduler.serve()` & `localpost.scheduler.aserve()` (just use `Host` instead)

## [0.3.0] - 2025-03-12

### Changed

- Renamed (from `justscheduleit` to `localpost`)
- Hosting reworked: Host (one service) & AppHost (many services)
- Scheduler: internals reworked completely

## [0.2.0] — 2024-10-03

### Added

- Batch trigger

### Fixed

- Safer async generators handling

## [0.1.0] — 2024-09-30

### Added

- Hosting foundation for the scheduler
- Scheduler itself
