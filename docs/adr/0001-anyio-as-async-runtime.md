# 0001 — AnyIO as async runtime

- **Status:** Accepted
- **Date:** 2026-05-07 (backfilled — decision predates the ADR practice)

## Context

The framework needs an async runtime for the hosting layer, scheduler,
async HTTP handlers, and the ASGI/RSGI bridges. Two practical choices in
the Python ecosystem:

- **Raw asyncio** — broadest compatibility, biggest ecosystem of
  libraries, the Python "default."
- **AnyIO** — a thin abstraction over asyncio *and* Trio. Provides
  structured concurrency primitives (`TaskGroup`, cancel scopes,
  `to_thread`) with the same API on both backends.

Forces in play:

- We want **structured concurrency** as the default — task groups and
  cancel scopes, not bare `asyncio.create_task` with manual lifetime
  bookkeeping.
- We want users to be able to run on **Trio** if they prefer it.
  `asyncio` is fine for many workloads, but Trio's strict structured
  concurrency catches a class of bugs (orphaned tasks, surprising
  cancellation propagation) that asyncio is more permissive about.
- The framework's surface is small and we control all internal async
  code. We don't need to consume large async libraries that are
  asyncio-specific.
- We pay a thin abstraction cost (one extra import layer) but gain
  portability between two backends with one codebase.

## Decision

All async code under `localpost/` uses **AnyIO**, not raw `asyncio`.
The code runs on either asyncio or Trio depending on what
`anyio.run` is given (the hosting layer picks via
`choose_anyio_backend`, defaulting to asyncio for compatibility).

We do not vendor an asyncio-only fast path. If a feature can't be
expressed in AnyIO, we either contribute upstream or design around it.

## Consequences

- Public API surfaces AnyIO types where backend-specific types would
  otherwise leak (`anyio.abc.TaskGroup`, `anyio.Event`).
- Tests use `anyio_mode = "auto"` in `pyproject.toml`, which runs every
  async test under both asyncio and Trio. Catches portability bugs at
  CI time.
- Documentation phrases everything in AnyIO terms ("structured
  concurrency", "task group", "cancel scope") rather than asyncio-isms
  ("event loop", "Task", "shield").
- Some asyncio-only third-party libraries (uvicorn, hypercorn,
  grpc.aio) are still consumed via adapters in
  `localpost/hosting/services/` — those adapters explicitly pin the
  backend to asyncio.
- We don't get the "use any random asyncio library" ergonomics. In
  practice this rarely bites, because the framework's job is to host
  user code, not to consume async libraries itself.
