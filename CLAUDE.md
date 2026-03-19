# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

LocalPost is a Python async framework providing:
- **Hosting** - App lifecycle management, to orchestrate multiple services
- **In-process task scheduler** - Lightweight scheduler for background tasks
- **Consumers** - Lightweight consumer services for various message brokers (Kafka, SQS, PubSub, in-memory channels, etc.)
- **HTTP Server** - Lightweight HTTP server for sync apps

Python 3.12+ required. Built on AnyIO for structured concurrency (works with asyncio and Trio).

## Development Commands

```bash
just deps              # Install all dependencies (uses UV)
just format            # Format code with ruff
just types             # Check types (using ty)
just tests             # Run all tests with coverage
just unit-tests        # Unit tests only (exclude integration)
just integration-tests # Integration tests (parallel with pytest-xdist)
just check FILE        # Check single file (ruff + ty)
```

Run a single test:
```bash
pytest tests/path/to/test.py::test_function -v
```

## Architecture

### Module Structure

```
localpost/
├── __init__.py              # Entry: Result, debug, __version__
├── _utils.py                # Core utilities (Result, Event, MemoryStream)
├── _debug.py                # Debug utilities
├── _otel_utils.py           # OpenTelemetry utilities
├── scopes.py                # Scope/DI container for resource management
├── threadtools.py           # Thread sync primitives (CancellableLock, Channel, Semaphore)
│
├── hosting/                 # App lifecycle management, service orchestration
│   ├── __init__.py         # Exports: service, run, serve, current_service, ServiceState, etc.
│   ├── _host.py            # Core: ServiceLifetime, ServiceLifetimeView
│   ├── middleware.py        # Middleware (shutdown_on_signal, etc.)
│   └── services/            # Hosting adapters (ASGI, gRPC, Hypercorn, Uvicorn)
│
├── scheduler/              # In-process task scheduler
│   ├── __init__.py         # Exports: Scheduler, Task, ScheduledTask, every, after, etc.
│   ├── _scheduler.py       # Task and Scheduler implementation
│   ├── _cond.py            # Condition/trigger implementations (Every, After)
│   ├── _trigger.py         # Trigger decorators (delay, take_first)
│   └── cond/cron.py        # Cron expression support
│
├── consumers/              # Message consumer services
│   ├── channel.py          # In-memory channel consumer
│   ├── pubsub.py           # Google Cloud Pub/Sub
│   ├── stream.py           # AnyIO stream consumer
│   └── stdlib_queue.py     # stdlib queue.Queue consumer
│
├── http/                   # HTTP server and routing
│   ├── server.py           # WSGI server (h11-based)
│   ├── router.py           # Routing (URITemplate, Router, RequestCtx)
│   ├── config.py           # Server configuration
│   ├── wsgi.py             # WSGI app wrapping
│   └── _service.py         # HTTP service integration with hosting
│
└── openapi/                # OpenAPI/Swagger support
    ├── app.py              # OpenAPI app builder
    ├── spec.py             # OpenAPI spec generation
    ├── converters.py       # Request/response converters
    ├── msgspec.py          # msgspec serialization
    ├── pydantic.py         # Pydantic validation
    ├── sse.py              # Server-Sent Events
    └── _docs.py            # API documentation UI
```

Files prefixed with `_` contain internal implementations. Public APIs are exported through `__init__.py`.

### Key Patterns

**Hosting/Services**: Services decorated with `@hosting.service` have lifecycle states (Starting → Running → ShuttingDown → Stopped). Middleware decorators for cross-cutting concerns. Context variable provides service lifetime.

**Scheduler**: Tasks triggered by conditions (`every`, `after`, `cron`). Triggers composable with operators (`//`, `>>`). Results wrapped in `Result[T]` (Ok/Failure).

**Consumers**: Generic handler interface (sync/async callables). Stream-based API with message broker abstraction (Kafka, SQS, Pub/Sub, in-memory channels).

**HTTP/OpenAPI**: WSGI server on h11, URI template routing with path parameter extraction. OpenAPI layer for spec generation, validation (Pydantic/msgspec), and docs UI.

### Conventions

- Use AnyIO for async work (structured concurrency)
- **Public API**: Full type annotations, checked with ty (`just types`)
- **Internal API**: Types only where they improve readability

## Testing

- Unit tests: `tests/` directory, run with `pytest -m "not integration"`
- Integration tests: Marked with `@pytest.mark.integration`, use testcontainers
- Async tests use `anyio[test]` pytest plugin
