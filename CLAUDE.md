# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project overview

LocalPost is an async Python framework (Python 3.12+) for building long-running
processes. Five pillars:

- **hosting** *(stable)* — service lifecycle + orchestration (start/stop/signals).
- **scheduler** *(stable)* — in-process, composable task scheduler.
- **http** *(stable)* — lightweight sync HTTP/1.1 server.
- **di** *(stable)* — small `.NET`-style IoC container with scopes.
- **experimental.consumers** *(experimental)* — message broker consumers (channel, stream, queue, Pub/Sub; more planned).
- **experimental.openapi** *(experimental)* — type-driven OpenAPI layer on top of `http`.

Built on AnyIO (runs on asyncio or Trio).

**Stability note:** "stable" modules have settled public APIs — avoid
breaking changes unless explicitly asked. Experimental modules live under
``localpost.experimental.`` so the import path itself flags them; they're
usable but their shape is still evolving — refactor freely, and flag
breaking changes in the CHANGELOG.

## Development commands

From `justfile`:

```bash
just deps              # uv sync --all-groups --all-extras
just deps-upgrade      # uv lock --upgrade && sync
just format            # ruff check --fix + ruff format on localpost/
just format-all        # same, also on examples/ and tests/
just types             # ty check localpost
just types-strict      # ty check localpost (strict mode)
just types-all         # ty over localpost + examples + tests
just type-coverage     # basedpyright --verifytypes on the public API
just tests             # pytest with coverage (all tests)
just unit-tests        # pytest -m "not integration"
just integration-tests # pytest -m "integration" -n auto (testcontainers)
just check FILE        # ruff check --fix + ty check for one file
just why PACKAGE       # inverse dep tree for a package
```

Single test:

```bash
pytest tests/path/to/test.py::test_function -v
```

## Architecture

```
localpost/
├── __init__.py          # Re-exports: Result, debug, __version__
├── _utils.py            # Result, Event, MemoryStream, delay helpers
├── _debug.py
├── _otel_utils.py
├── threadtools.py       # Sync primitives (CancellableLock, Channel, cancellable_semaphore)
│
├── hosting/             # Service lifecycle + orchestration
│   ├── _host.py         # ServiceLifetime, serve, run, @service, current_service
│   ├── middleware.py    # shutdown_on_signal, start_timeout
│   └── services/        # Adapters: _asgi, uvicorn, hypercorn, grpc
│
├── scheduler/           # Composable task scheduler
│   ├── _scheduler.py    # ScheduledTask, ScheduledTaskTemplate, scheduled_task, run
│   ├── _cond.py         # Every, After, AfterAll; every/after/after_all; delay, take_first
│   ├── _trigger.py      # Trigger decorators (WIP)
│   └── cond/cron.py     # cron(...) trigger — needs [cron] extra
│
├── di/                  # IoC container
│   ├── _services.py     # ServiceRegistry, ServiceProvider, AppContext
│   ├── flask.py         # Flask integration (RequestContext per request)
│   └── quart.py         # Quart integration (stub)
│
├── http/                # Lightweight HTTP/1.1 server (h11-based, sync I/O loop)
│   ├── server.py        # start_http_server, HTTPReqCtx, RequestHandler
│   ├── router.py        # URITemplate (RFC 6570 L1), Router, RequestCtx
│   ├── wsgi.py          # wrap_wsgi()
│   ├── config.py        # ServerConfig
│   └── _service.py      # @hosting.service wrappers (http_server, wsgi_server)
│
└── experimental/        # Unstable APIs — wrapped in their own subpackage as a marker
    ├── consumers/       # Message broker consumers
    │   ├── channel.py       # in-memory channel
    │   ├── stream.py        # AnyIO ObjectReceiveStream
    │   ├── stdlib_queue.py  # queue.Queue / SimpleQueue
    │   ├── pubsub.py        # Google Cloud Pub/Sub (in-progress; imports are broken)
    │   └── _utils.py        # SyncHandler / AsyncHandler / AnyHandler types
    └── openapi/         # Type-driven OpenAPI framework
        ├── app.py           # HttpApp, FromPath/Query/Header/Body, OpResult hierarchy
        ├── spec.py          # OpenAPI spec dataclasses
        ├── converters.py
        ├── pydantic.py      # Pydantic body/result converters
        ├── msgspec.py       # msgspec converters (stub)
        ├── sse.py           # Server-Sent Events
        ├── _docs.py         # Swagger UI / ReDoc / Scalar HTML templates
        └── DESIGN.md        # Deeper design notes
```

Files prefixed with `_` are internal; public API is re-exported from each
module's `__init__.py`.

## Conventions

- **AnyIO everywhere.** Structured concurrency; no raw `asyncio`. Works on both
  asyncio and Trio backends.
- **Public API is fully typed** — verified with `just types` (using `ty`) and
  `just type-coverage` (using `basedpyright --verifytypes`).
- **Internal code** uses types where they aid readability; not strictly required.
- **Errors as values** — `Result[T]` (Ok / failure) flows through scheduler tasks
  and arg resolvers.
- **Ruff** with `line-length = 120`, `pyupgrade.keep-runtime-typing = true`.
- **Sync + async duality** — consumers and scheduler accept both; sync callables
  are offloaded to threads via `anyio.to_thread`.
- Docstrings: Google convention (per ruff config).

## Testing

- Unit tests: default `pytest` invocation, marker `not integration`.
- Integration tests: marked `@pytest.mark.integration`, use `testcontainers`
  (LocalStack, NATS, Google Pub/Sub emulator), run in parallel via
  `pytest-xdist` (`-n auto`).
- `anyio_mode = "auto"` in `pyproject.toml` — async tests use `anyio[test]`.

## Workflow rules

- After editing any file under `localpost/`, run `just check <file>` (ruff + ty).
  This catches lint and type regressions early.
- Never skip hooks on commit. Prefer a new commit over `--amend`.
- Treat `_`-prefixed modules as internal; change them freely, but don't import
  from `_*` outside the module they live in.

## Module deep-dives

For more detail on each module, see:

@localpost/hosting/README.md
@localpost/scheduler/README.md
@localpost/di/README.md
@localpost/http/README.md
@localpost/experimental/consumers/README.md
@localpost/experimental/openapi/README.md
