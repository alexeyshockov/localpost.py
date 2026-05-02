# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project overview

LocalPost is an async Python framework (Python 3.12+) for building long-running
processes. Four pillars:

- **hosting** — service lifecycle + orchestration (start/stop/signals).
- **scheduler** — in-process, composable task scheduler.
- **http** — lightweight sync HTTP/1.1 server.
- **di** — small `.NET`-style IoC container with scopes.

Built on AnyIO (runs on asyncio or Trio).

**Stability note:** all four modules have settled public APIs — avoid
breaking changes unless explicitly asked.

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
└── http/                # Lightweight HTTP/1.1 server (h11-based, sync I/O loop)
    ├── server.py        # start_http_server, HTTPReqCtx, RequestHandler
    ├── router.py        # URITemplate (RFC 6570 L1), Router, RequestCtx
    ├── wsgi.py          # wrap_wsgi()
    ├── config.py        # ServerConfig
    └── _service.py      # @hosting.service wrappers (http_server, wsgi_server)
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
- **Sync + async duality** — the scheduler accepts both; sync callables are
  offloaded to threads via `anyio.to_thread`.
- Docstrings: Google convention (per ruff config).

## Testing

- Unit tests: default `pytest` invocation, marker `not integration`.
- Integration tests: marked `@pytest.mark.integration`, run in parallel via
  `pytest-xdist` (`-n auto`).
- `anyio_mode = "auto"` in `pyproject.toml` — async tests use `anyio[test]`.

## Workflow rules

- After editing any file under `localpost/`, run `just check <file>` (ruff + ty).
  This catches lint and type regressions early.
- Never skip hooks on commit. Prefer a new commit over `--amend`.
- Treat `_`-prefixed modules as internal; change them freely, but don't import
  from `_*` outside the module they live in.

## Module deep-dives

For more detail on each module, read its README on demand:

- `localpost/hosting/README.md`
- `localpost/scheduler/README.md`
- `localpost/di/README.md`
- `localpost/http/README.md`
