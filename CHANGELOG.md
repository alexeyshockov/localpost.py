# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.0] - 2026-05-08

**Effectively a rewrite from 0.4.0.** The project's focus — long-running
async Python processes built on AnyIO — is unchanged, but most internals
and a number of public APIs have changed. The core pillars (`hosting`,
`scheduler`, `http`, `di`) are the stable public surface, verified with
`ty` and `basedpyright --verifytypes`. A new `localpost.openapi` module
ships alongside them — a type-driven HTTP framework with OpenAPI 3.2
generation built in. Several exploratory modules from the 0.4 line
(`flow`, `consumers`, `experimental`) are gone; they may return as a
separate package once the design settles.

0.5.0 was drafted in `CHANGELOG.md` but never released. Its still-relevant
items are folded into this entry; the consumer/SQS/Kafka rework is dropped
along with the rest of `experimental`.

Python 3.12+ is now required (was 3.10+).

### Added

- **`localpost.http`** — small h11-based HTTP/1.1 server with a single
  selector thread and pluggable parser backend (`h11` by default,
  `httptools` via the `[http-fast]` extra). Driven by
  `start_http_server(config, handler)`. Includes:
  - `HttpApp` — decorator-driven framework on top of the lean `Router`
    (`@app.get`, `@app.post`, …): parameter injection, response
    conversion (str / bytes / dict / list / `Response` / `None`),
    worker-pool dispatch, app-level + per-route middleware.
  - `Router` — minimal RFC 6570 Level-1 URI template dispatcher; attaches
    `RouteMatch` to `ctx.attrs["route_match"]`, exposed via
    `route_match(ctx)`.
  - WSGI bridge (`wrap_wsgi`, `to_wsgi`), ASGI bridge (`to_asgi`), RSGI
    bridge (`to_rsgi` + `HostRSGIApp` for Granian deployments).
  - `read_body` / `aread_body` body helpers, `static_handler`,
    `compress_handler` (gzip stdlib; brotli via the `[http-compress]`
    extra).
  - `thread_pool_handler(handler, executor)` /
    `streaming_pool_handler(handler, executor)` — opt-in worker-pool
    offload of handlers and streaming uploads. The executor is
    caller-owned; the wrapper holds an internal `TaskGroup` for drain
    semantics but does not own the executor lifecycle.
  - `HttpApp.service(executor=...)` and `openapi.HttpApp.service(executor=...)`
    accept an open `Executor`. When omitted, the service opens an
    `AsyncWorkerExecutor` on the hosting portal so handlers get
    `from_thread.check_cancelled` support automatically.
  - `HTTPReqCtx.attrs` — mutable per-request state for cross-cutting
    concerns (auth, tracing, rate-limit, body cache).
  - **Free-threaded CPython 3.14t support** — verified end-to-end. ~3x
    RPS jump at `selectors=1` (httptools plaintext: 12,563 → 36,208 RPS)
    just from removing the GIL. The pure-Python `[http]` (h11) backend
    is no-GIL-clean today; `[http-fast]` (httptools) needs 0.8+ to avoid
    auto-re-enabling the GIL on import.
  - **Multi-selector single-process** via `ServerConfig.selectors > 1`
    on Linux (`SO_REUSEPORT`). macOS does not load-balance accepts and
    is a no-op there pending an accept-dispatch design.
  - Neutral wire types (`Request`, `Response`, `InformationalResponse`,
    `BodyTooLarge`) — the public API does not leak `h11` or `httptools`.
- **Async HTTP context surface** — `AsyncHTTPReqCtx` Protocol +
  `AsyncRequestHandler` type. `to_asgi` / `to_rsgi` adapters expose the
  same handler shape over async transports, so the same `HttpApp` can run
  under Granian or Uvicorn or the in-tree sync server.
- **`localpost.threadtools`** — primitives for thread-bridging code, built
  on plain locks (no AnyIO loop required for the sync pieces):
  - `Channel` — typed, thread-safe queue with separate `SendChannel` /
    `ReceiveChannel` halves, `timeout=` on `put` / `get`, `get_nowait`,
    and broadcast-on-close so cloned receivers all observe `EndOfStream`
    / `ClosedResourceError`. Capacity modes: unbounded (`None`),
    rendezvous (`0`), bounded (`N>0`).
  - `Executor` protocol — single `submit(fn, *args, **kwargs) -> Future`
    contract. Three implementations:
    - `WorkerExecutor` — sync `with`, channel-backed pool of plain
      `threading.Thread` workers, lazy spawn with idle-timeout
      self-exit, no event loop needed.
    - `AsyncWorkerExecutor` — `async with`, same channel/lazy-spawn
      shape but workers run via `anyio.to_thread.run_sync(...,
      abandon_on_cancel=False)` so user code can call
      `anyio.from_thread.check_cancelled`. Cancel granularity is
      *per-worker* (one worker handles many tasks).
    - `AsyncExecutor` — `async with`, fresh AnyIO task per submit gated
      by an always-on `CapacityLimiter` (`math.inf` = no cap). Cancel
      granularity is *per-task* (`Future.cancel()` propagates).
    The async variants take a caller-owned `BlockingPortal` and hold an
    internal `anyio.TaskGroup`; `stop()` cancels every in-flight task.
  - `TaskGroup` — Trio-style structured concurrency over an `Executor`.
    Tracks `Future`s, drains in `__exit__`, surfaces failures as a
    `BaseExceptionGroup` (body + task exceptions merged, deduplicated
    by identity).
  - All three executors snapshot `contextvars.Context` per submit,
    matching `asyncio.to_thread` / Trio / AnyIO spawn semantics.
- **`localpost.di`** — `.NET`-style scoped IoC container
  (`ServiceRegistry`, `ServiceProvider`, `AppContext`) with a Flask
  integration that scopes services per request.
- **Hosting middleware** — `shutdown_on_signal()` and `start_timeout(...)`,
  composable around any `ServiceF`. New `+` and `>>` operators for
  combining and wrapping services.
- **`ServiceLifetimeView.portal`** exposes the hosting layer's per-app
  `BlockingPortal`. Lets services compose `AsyncWorkerExecutor` /
  `AsyncExecutor` against the same loop without opening a redundant
  portal — the pattern HTTP / openapi `HttpApp.service(...)` use to
  default their internal pool.
- **`HostRSGIApp`** — host an RSGI app under `localpost.hosting` so it
  participates in the same lifecycle / signals as everything else.
- **`localpost.debug`** — context manager to attach AnyIO-aware debug
  hooks during development.
- **`hosting.services`** adapters — `uvicorn`, `hypercorn`, `grpc`, and a
  generic `_asgi`. Each runs the underlying server as a hosted service
  with proper start / stop semantics.
- **Scheduler trigger composition** — operator-based combinators
  (`every("1m") // delay((0, 10))`), `take_first(n)`, `cron(...)` (via
  the `[cron]` extra). Sync handlers are auto-offloaded to threads via
  `anyio.to_thread`. Trigger middleware is now async-generator based
  (`trigger_factory_middleware`).
- **`localpost.openapi`** (`[openapi]` extra) — type-driven HTTP framework
  with OpenAPI 3.2 generation, on top of `localpost.http`. FastAPI-style
  decorator API where the spec and runtime handling are derived from the
  *same* type annotations, including union return types for response
  shapes (`Book | NotFound[str]`). msgspec for encoding / decoding /
  schema generation by default; pydantic and `attrs` recognised
  automatically when present (`[openapi-attrs]` adds `attrs` + `cattrs`).
  Sync (`HttpApp`) and async (`HttpAsyncApp`) flavours; OpenAPI-aware
  middleware can contribute security schemes and extra responses.
- **Documentation site** — Zensical-based site built from per-module
  READMEs (`mkdocs.yml`); seed ADRs and design notes under `docs/`.

### Changed (BREAKING)

- **Python 3.12+** required; 3.10 / 3.11 classifiers dropped.
- **Hosting fully rewritten.** `Host` and `AppHost` are gone. The new
  surface is the `@service` decorator → `ServiceF`, top-level
  `serve` / `run` / `run_app` entry points, structured `ServiceState`
  (`Starting → Running → ShuttingDown → Stopped`), and `+` / `>>`
  operators for service composition. See `localpost/hosting/README.md`.
- **`Router` is a lean dispatcher**, not a self-contained framework.
  The old `RequestCtx` / `Response` / `(RequestCtx) -> Response` shape is
  gone from `localpost.http.router`; `Router.as_handler()` returns a
  plain `RequestHandler` that attaches a `RouteMatch` and delegates.
  Pythonic helpers (decorators, response conversion, param injection)
  live on the new `HttpApp`. Pair with `wrap_wsgi` if you need WSGI
  output.
- **Scheduler internals reworked.** `ScheduledTask` /
  `ScheduledTaskTemplate` / `Task` / `Scheduler`, declarative triggers
  (`every`, `after`, `after_all`, `cron`); the sync/async-handler duality
  is preserved.
- **`localpost.threadtools` reshaped.** The module is now a package
  (`localpost/threadtools/`); `ThreadTaskGroup` was renamed to
  `TaskGroup`. The rest of the surface diverges from any previous
  drafts:
  - `Channel.create(...)` no longer takes `check_cancelled`; instead
    `put` / `get` take an explicit `timeout=`. `close()` broadcasts to
    every cloned waiter.
  - `CancellableLock`, `cancellable_condition`, `cancellable_semaphore`
    are removed — cancellation moves up a layer (executor / task group
    / caller polls timeouts).
  - `TaskGroup` no longer relies on an ambient `thread_pool()`
    contextvar. It takes an `Executor` explicitly:
    `TaskGroup(executor)`. Spawning runs through `executor.submit`;
    drain and error-folding semantics are unchanged.
  - The old `ThreadPool` / `thread_pool()` async context manager is
    gone, replaced by the three explicit `Executor` implementations
    (see the Added section).
- **`localpost.http.thread_pool_handler` / `streaming_pool_handler`
  signature.** Both now require an `executor` argument:
  `thread_pool_handler(handler, executor)`. Caller owns the executor
  lifetime. The previous version auto-opened an ambient pool via the
  removed `thread_pool()` contextvar.
- **HTTP server** does not leak parser types into the public API.
  `HTTPReqCtx.request` is `localpost.http.Request` (was `h11.Request`);
  `HTTPReqCtx.start_response` and `complete` accept
  `localpost.http.Response` / `InformationalResponse`. Field shapes match
  h11's, so the migration is mechanical (`from localpost.http import
  Response` and replace).
- **HTTP backend selection** lives on `ServerConfig.backend: Literal[
  "h11", "httptools"] = "h11"`. There is one entry point —
  `start_http_server` — and one hosted-service wrapper — `http_server`.
- **`http_server` no longer owns a worker pool.** The `max_concurrency`
  kwarg is gone from `http_server`, `flask_server`, and `wsgi_server`;
  wrap your handler with `thread_pool_handler` to opt back into a pool
  (typical for blocking WSGI / Flask handlers).
- **HTTP/1.1 pipelining is no longer supported.** Pipelined clients are
  served sequentially.
- **Internal typing modernised** to PEP 695 across `_utils`, `scheduler`,
  and `hosting`. No public-API change.

### Removed

- **`localpost.flow`** — too complicated; the data-flow surface lives on
  through the scheduler's trigger composition.
- **`localpost.experimental`** — both `experimental.consumers` (channel /
  stream / queue / Pub/Sub) and `experimental.openapi` are removed.
  Corresponding extras (`[sqs]`, `[kafka]`, `[nats]`, `[http-openapi]`)
  and `examples/consumers/`, `examples/openapi/`, `tests/experimental/`
  are gone too. May return as a separate package once the design settles.
- **`scheduler.serve()` / `scheduler.aserve()`** — use `hosting.run` /
  `run_app` instead.
- **`localpost.flow_ops`** (had been merged into `flow` for 0.4) — gone
  along with `flow`.
- Internal helpers that were unused everywhere: `_utils.NO_OP_TS`,
  `AsyncContextManagerAdapter`, `Switch`, `MemorySendStream`'s
  `send_or_drop_from_thread` / `send_or_drop` (so `MemoryStream.create()`
  now returns the bare AnyIO stream pair), and
  `hosting._serve_and_observe`.

### Fixed

- **Scheduler keeps its loop alive across handler exceptions** — a single
  failing run no longer terminates the schedule.
- **`Task.subscribe` after the task has finished** raises a clear error
  instead of silently hanging; graceful mid-iteration shutdown is
  covered.
- **`TaskGroup.__exit__`** deduplicates exceptions that propagate via
  both the body and a child task by identity, so each appears at most
  once in the resulting `ExceptionGroup`.
- **HTTP send path** — non-blocking `send` with a blocking-with-timeout
  fallback on `BlockingIOError`; per-request `settimeout` calls dropped
  from the borrow boundary (saves two `fcntl` per request, +21–32% RPS
  on the bench's hot path).
- **`UvicornService`** no longer crashes the whole app if the embedded
  server fails to start (carried over from the unreleased 0.5 draft).

### Performance

- HTTP `Router` restructured into a lean dispatcher (+35% RPS on the
  bench's hot path).
- See `benchmarks/macro/http/PERF_FINDINGS.md` for per-phase notes and numbers.

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
