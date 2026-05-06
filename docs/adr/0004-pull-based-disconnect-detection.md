# 0004 — Pull-based client-disconnect detection

- **Status:** Accepted
- **Date:** 2026-05-07 (backfilled — decision predates the ADR practice)

## Context

When a worker thread holds a borrowed connection (the BORROWED state
in [`docs/design/connection-model.md`](../design/connection-model.md)),
we still need to detect that the client closed the socket. Two
reasons:

- Long-running handlers (SSE generators, large computed responses)
  should short-circuit when the client is gone, rather than burn CPU
  / DB time producing data nobody will read.
- The hosted service's shutdown signal needs to reach in-flight
  requests so they can cancel cleanly.

Originally the server used a **push-based** design. While a worker
held a borrowed connection, the selector kept the socket registered
in a third connection mode — call it "watchdog" — separate from
TRACKED and BORROWED. The selector watched only for read-readiness
on the watchdog socket; when it fired, the selector inferred peer
FIN (no more bytes were expected from the client mid-response) and
posted a disconnect event to the worker.

Forces in play:

- The worker and selector both touch the same socket. The watchdog
  mode meant a third concurrent reader, which created races: the
  worker reading body bytes while the selector polled the same fd
  could trigger spurious "disconnected" callbacks, or worse, race
  the actual response write.
- The state machine grew a third node and several new edges. Every
  transition had to handle "what if a watchdog event fires *during*
  this transition" — fiddly, and a place we shipped real bugs.
- We were paying selector-side syscalls and book-keeping for a
  signal that's only consulted occasionally inside long handlers.
  The workload that benefits most (SSE generators) already loops
  per-event; making the check explicit and on-demand fits naturally.

## Decision

Disconnect detection moves to a **pull-based** model. While a worker
holds a borrowed connection:

- `HTTPReqCtx.disconnected` does a non-blocking
  `recv(1, MSG_PEEK | MSG_DONTWAIT)` on the request socket. `b""`
  means peer FIN; the flag is sticky once `True`.
- `check_cancelled()` raises `RequestCancelled` if the client
  disconnected (same `MSG_PEEK` check) or the hosted service is
  shutting down. Sync handlers without `ctx` in scope can call it
  from anywhere on the request thread.
- Handlers doing regular I/O surface disconnects naturally via
  `EPIPE` / `ECONNRESET` from the socket write — no explicit check
  needed.

The selector no longer has a "watchdog" mode. The connection has
two states (TRACKED / BORROWED) plus closed.

## Consequences

- The connection state machine collapses from three nodes to two.
  Documented as the load-bearing diagram in
  [`docs/design/connection-model.md`](../design/connection-model.md).
- One syscall per `check_cancelled()` call, but only when the
  handler actually asks. Idle handlers pay nothing.
- The contract is symmetric across sync and async: sync uses
  `MSG_PEEK`; async transports flip `ctx.disconnected` on
  `http.disconnect`. Same field name, same semantics.
- The WSGI bridge has no socket handle, so it always reports
  `disconnected = False` and surfaces disconnects via
  `BrokenPipeError` from the host's per-chunk write. Documented as
  an asymmetric corner of the surface table in the connection model
  doc.
- Handlers that never poll and never write (rare — they'd have to
  block on something else, like a queue) won't notice peer-gone.
  That's fine: the same handler shape would also miss a service
  shutdown signal, and the answer is the same — call
  `check_cancelled()` periodically.
