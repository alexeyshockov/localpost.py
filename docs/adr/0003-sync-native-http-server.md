# 0003 — Native HTTP server stays sync-only

- **Status:** Accepted
- **Date:** 2026-05-07 (backfilled — decision predates the ADR practice)

## Context

`localpost.http.RequestHandler` is `Callable[[HTTPReqCtx], None]` —
synchronous. The selector accepts connections, parses HTTP/1.1, and
either dispatches the request inline on the selector thread (e.g. for
a 404 from `Router`) or hands it off to a worker thread (when the user
composes `thread_pool_handler` into the chain).

Recurring pressure to add an async path inside this server:

- "What if I want async DB calls in my handler?"
- "Should `RequestHandler` accept either sync or async functions?"
- "Could we run the selector inside an event loop?"

Forces:

- Async HTTP servers in Python are a solved problem. uvicorn,
  hypercorn, and granian are mature, well-tested, and out-perform
  anything we'd build. Async users have first-class options today.
- We integrate with those servers via
  `localpost.http.asgi.to_asgi(handler)`, which adapts an
  `AsyncRequestHandler` (`Callable[[AsyncHTTPReqCtx], Awaitable[None]]`)
  to ASGI 3. The hosting adapters in `localpost.hosting.services/`
  plug them into `run_app` cleanly.
- Adding a parallel async path inside the native server would mean a
  second connection state machine, a second parser drive loop, a
  second body-reading contract — all maintained alongside the sync
  ones. The selector-thread + thread-pool model is small precisely
  because there's only one path.
- The sync server's design (blocking sockets, `selectors` poller,
  TRACKED/BORROWED state machine) is what makes it small and
  free-threaded-ready. An event loop inside it would change all of
  that.

## Decision

The native server in `localpost.http` is sync-only. `RequestHandler`
will not be widened to accept async callables. There is **no plan**
to add an async path here.

Async handlers are reached via the ASGI bridge
(`localpost.http.asgi.to_asgi`) plugged into uvicorn / hypercorn /
granian. The handler still expresses the same conceptual contract
(read request, write response) — just an async-flavoured Protocol
(`AsyncHTTPReqCtx`) instead of the sync one.

Both Protocols share the data side (`request`, `body`, `attrs`,
addrs, `disconnected`) and the terminal write methods (`complete`,
`stream`, `sendfile`, `receive`), so a handler that only touches the
core surface is portable between transports — see
[`docs/design/connection-model.md`](../design/connection-model.md#sync-vs-async-request-context-surface).

## Consequences

- Anyone who needs an async HTTP server uses `to_asgi(handler)` +
  uvicorn / hypercorn / granian. We point them there explicitly.
- The native server stays small (~540 lines of sync code, plus a
  parallel httptools backend — see ADR-0002). Free-threaded CPython
  builds are a clean target because there's no event loop to
  reason about.
- We don't compete with uvicorn / hypercorn / granian on async perf.
  We compete on small surface area, predictable threading, and
  composability with the hosting layer.
- The framework's user-facing surface (`HttpApp` / `HttpAsyncApp`)
  splits along sync/async like `httpx.Client` / `httpx.AsyncClient`.
  Choose flavour per app; don't mix.
