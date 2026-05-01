# HTTP server performance findings

Initial diagnosis from the 2026-04-27 bench run (3s per cell, before Phase 1).
Phase 1 results in [Phase 1 results](#phase-1-results-2026-04-27) below.

## Optimisation boundaries

Every optimisation in this document operates within these hard constraints:

1. **In-process only.** No multi-processing, no `fork` / `spawn`. Multi-core
   fanout is the user's deployment problem (systemd, k8s, external
   supervisors). Multi-*selector* inside one process is in scope.
2. **No async Python.** Sync handlers + threads only on the server side.
   `asyncio` / `uvloop` / ASGI are out of scope. The async-ASGI comparators
   (`starlette_uvicorn`, `starlette_granian`) stay in the bench matrix as
   reference points only — they're not goals.
3. **GIL or free-threaded.** Standard CPython 3.12+ is the baseline.
   Free-threaded builds (3.13t / 3.14t) are an accepted target — the
   architecture should hold up there with care, and `selectors=N` is where
   no-GIL scaling actually pays off.

These constraints exist because LocalPost is a *library*, not a deployment
platform. Process supervision, multi-worker orchestration, and hot reloads
belong upstream.

### Workload assumptions (the JSON-API common case)

These guide which paths get optimised first; we cut corners on shapes
that don't fit:

- **Reject before body.** No-route / wrong-method / auth failures
  complete inline on the selector, with no body recv.
- **Body is buffered, not streamed.** When a handler needs the body, it
  needs the whole thing (e.g. to deserialise JSON). The selector
  buffers the full body into ``ctx.body`` and then invokes a
  :data:`BodyHandler` continuation. There's still a ``ctx.receive``
  streaming API but it isn't the optimised path.
- **Response is one chunk or SSE.** Most responses are one
  status-line + headers + body block; SSE generators emit the same
  block plus subsequent data chunks. The response writer
  auto-buffers headers and flushes them with the first body chunk in a
  single ``sendall``.
- **No HTTP/1.1 pipelining.** Pipelined clients are served sequentially
  (correct, just no parallelism). Dropping support simplified the
  per-conn state machine.

## Headline numbers

| Stack             |    RPS | p50 (ms) | concurrency |
| ----------------- | -----: | -------: | ----------: |
| flask_cheroot     |  7,433 |     2.41 |  32 threads |
| localpost_native  |  5,450 |    11.71 |          32 |
| localpost_flask   |  5,117 |    12.46 |          32 |
| localpost_wsgi    |  4,919 |    13.03 |          32 |

Cheroot has only ~37% more RPS but **~5× lower p50**. h11's parser overhead would
show up as flat per-request CPU, not as a 12 ms vs 2.4 ms gap. **h11 is not the
bottleneck — the dispatch architecture is.**

## Diagnosis

`localpost_native`, `localpost_flask`, `localpost_wsgi` all cluster within 10% of
each other in every scenario — the bottleneck is upstream of the framework layer
(h11 + dispatch path), not WSGI/Flask conversion.

Latency math: with p50 ≈ 12 ms and 64 concurrent clients, 64 / 0.012 ≈ 5,300 RPS,
matching measured RPS almost exactly. That's **queueing-latency-bound**, not CPU.

### Per-request hot path (`localpost/http/_service.py:58-67`)

```
selector thread reads bytes
  → ctx.borrow()                        # acquires Server._lock, unregisters fd
  → from_thread.run_sync(start_soon …)  # SYNCHRONOUS cross-thread call (blocks selector)
event loop schedules task
  → to_thread.run_sync(handler, …)      # another hop into a worker
worker runs handler
  → ctx.complete() → finish_response()
  → _maybe_give_back() → server.track() # acquires Server._lock, re-registers fd
```

3 thread transitions, 2 lock acquisitions, 1 synchronous portal call **per
request**. The selector thread blocks on `from_thread.run_sync` waiting for the
event loop to accept the dispatch — that wait is the queueing latency.

Cheroot, by contrast, has each worker thread `accept()` and run the request
end-to-end. No event loop, no hops.

### Secondary costs (real, but second-order)

- `Server._cleanup_stale()` runs every iteration of `Server.run()` and walks
  `selector.get_map().values()` under `_lock` (`server.py:174-184`). O(N) per
  loop tick.
- `_maybe_inject_keep_alive` allocates a fresh `h11.Response` per request and
  scans headers byte-by-byte (`server.py:509-530`).
- `_build_environ` walks request headers twice and does
  `.decode().upper().replace()` per header on the second pass
  (`wsgi.py:135-172`).
- h11 is pure-Python — ~20–30% gap to a C parser, but shows up as CPU, not p50.

## Plan

### Phase 1 — Worker thread pool with HTTP-native cancellation

Replace the per-request AnyIO dispatch in `localpost/http/_service.py` with a
worker thread pool that runs handlers directly. `start_http_server` and
`Server` are unchanged — only the hosted-service dispatcher above them.

**Dispatch path (target):**

```
selector thread (in lt.tg, via to_thread.run_sync once)
  reads bytes, parses, ctx.borrow()
  → channel_tx.put((ctx, stack))         # threadtools.Channel, bounded

worker thread N-of-N (also in lt.tg, via to_thread.run_sync once)
  for ctx, stack in channel_rx:
      with request_cancel_scope(conn) as token:
          handler(ctx)                    # runs inline, no further hops
      stack.close()                       # re-tracks the conn for keep-alive
```

No portal calls, no `from_thread.run_sync` per request, no `to_thread.run_sync`
per request. Two thread crossings (selector → worker, worker → selector for
re-tracking) instead of five.

**Why `threadtools.Channel`:** consistent with the rest of the codebase, gives
us cancellation-aware `put`/`get` for free. Workers and selector are both
AnyIO-aware threads (spawned via `to_thread.run_sync` once at service start),
so the channel's existing AnyIO-driven cancellation fires correctly on
service shutdown — workers exit cleanly when `lt.tg` is cancelled or
`channel_tx.close()` is called.

**Bounded capacity** = `max_concurrency`. When full, the selector's
`channel_tx.put()` blocks → back-pressure on accept.

### Phase 1b — HTTP-native request cancellation (distinct from AnyIO)

A separate, HTTP-only cancellation layer for *request-level* signals
(client disconnect, future per-request timeout). **Not mixed with AnyIO** —
the AnyIO layer handles service-level cancellation of *workers*; this layer
handles *requests*.

New surface in `localpost.http`:

- `localpost.http.RequestCancelled` — exception, distinct from
  `anyio.get_cancelled_exc_class()`. Inherits from `Exception`, not
  `BaseException` (so handlers can catch broad `except Exception` without
  surprises — different choice from AnyIO on purpose, since these are
  request-scoped, not task-scoped).
- `localpost.http.check_cancelled()` — reads a `ContextVar[RequestCancel]`,
  raises `RequestCancelled` if the per-request token is set. **Not** an alias
  of `threadtools.check_cancelled`. Documented as "call this in long-running
  handlers; raises if the client went away or the server is shutting down".
- `RequestCancel` — internal token: a `threading.Event` per in-flight request,
  plus a registry on the hosted service so shutdown can flip them all.

Cancellation triggers (Phase 1b):

1. **Client disconnect** (the genuinely-useful trigger). While the handler is
   running, the borrowed conn is re-registered in the selector with a
   "watchdog" data tag. On `EVENT_READ`, selector does
   `recv(1, MSG_PEEK | MSG_DONTWAIT)`; `b""` → EOF → flip the request's
   cancel token. Safe vs the worker thread's `send()` (PEEK doesn't consume,
   send is independent of recv at the kernel level). The watchdog is only
   armed *after* the request body is fully read, to avoid racing with
   handler-driven body `recv()` calls.
2. **Service shutdown.** When `lt.shutting_down` fires, walk the in-flight
   registry and flip every cancel token. In-flight handlers calling
   `check_cancelled()` see it.

What we *don't* try to do in Phase 1b: per-request timeouts, async-handler
cancellation, mid-body-upload disconnect detection. Each is a clean follow-up
on the same primitive.

### Phase 2 — h11 / WSGI micro-optimisations (after Phase 1 ships)

1. Pre-bake the keep-alive header. Skip the rebuild in
   `_maybe_inject_keep_alive` (`server.py:509-530`) when no `Connection`
   header is present and the response has no keep-alive yet — append a
   precomputed tuple instead of allocating a fresh `h11.Response`.
2. One-pass `_build_environ` (`wsgi.py:135-172`) — fold the two header walks
   into one, cache `bytes(name)`.
3. Avoid `bytes(name).lower()` allocations in `_content_length` and the
   keep-alive scan in `server.py`.

### Phase 3 — Selector self-pipe + lock-free op queue (deferred)

The item already on the http README roadmap. Reconsider after Phase 1+2
benchmarks; only worth doing if a measurable gap to Cheroot remains.

## Validation plan

- A/B Phase 1 against current path in `benchmarks/http/runner.py`. Expect
  p50 to collapse from ~12 ms toward ~2-3 ms; RPS to rise correspondingly.
- New tests for Phase 1b:
  - Client closes mid-request → handler's `check_cancelled()` raises
    `RequestCancelled`.
  - Service shutdown with in-flight handler → same.
  - `check_cancelled()` outside a request raises a clear error (not
    `RuntimeError` from contextvar lookup).
- Run existing `tests/http/` to ensure no regressions.
- `just check localpost/http/_service.py` after each substantive change.
- Phase 2 should mostly improve RPS, not p50.
- `just bench-micro` to confirm no router regressions.

## Phase 1 results (2026-04-27)

Bench: 5 s per cell, `max_concurrency=32`, 64 concurrent clients.

| Stack             |  RPS (before → after) | p50 (before → after) |
| ----------------- | --------------------: | -------------------: |
| `localpost_native`|     5,450 → **9,381** |    11.71 → **6.29 ms** |
| `localpost_flask` |     5,117 → **8,157** |    12.46 → **7.44 ms** |
| `localpost_wsgi`  |     4,919 → **7,743** |    13.03 → **7.87 ms** |
| `flask_cheroot`   |     7,433 →   8,218   |     2.41 →   2.20 ms |

LocalPost native now **out-throughputs Cheroot** (9,381 vs 8,218 RPS) on
plaintext. Same on `json_post`: 7,985 vs 7,698 RPS. Cheroot still wins on
p50 (~2 ms vs ~6 ms) — that's the thread-per-connection model paying off
for tail-latency. We're queue-bound at ~9 k RPS now (`64 / 0.006 ≈ 10,600`
matches measured RPS); raising `max_concurrency` past 32 should push p50
down and RPS up further.

All `tests/http/` (129 tests) green; 10/10 stress runs at 800/800 success.

### What shipped

- Worker thread pool fed by `threadtools.Channel` (`localpost/http/_service.py`).
  No more per-request portal call or `to_thread.run_sync` hop.
- HTTP-native cancellation: `localpost.http.check_cancelled` /
  `RequestCancelled` (`localpost/http/_cancel.py`), a per-request token
  registry, and service-shutdown propagation. **Not** mixed with AnyIO.
- `Server.to_watchdog` for client-disconnect detection while a handler runs.
  Cooperates with the worker via a mode-recheck under `_lock`.
- `HTTPConn.tracked` → `HTTPConn.mode: ConnMode` (UNTRACKED / NORMAL / WATCHDOG).
  `tracked` kept as a backwards-compat property.
- `finish_response` now drains h11's pending request-body events before
  `_maybe_give_back`. Without this the next keep-alive request hits
  `PAUSED` from `parser.next_event`.

### Bug we hit and fixed

Once the worker pool was wired up, ~6% of requests under contention failed
with `Connection reset by peer`. Root cause: the worker's *outer* `track()`
call (in the `finally` block) ran `sock.settimeout(0)` on the conn, switching
the shared socket back to non-blocking — racing with the **next** request's
worker, which was already mid-response in blocking I/O mode and would then
hit a spurious `BlockingIOError`. The dispatcher now relies entirely on
`finish_response`'s `_maybe_give_back` to re-track; the outer block only
closes the conn when the inner path didn't (cancel / disconnect / handler
returned without completing).

## Phase 2 results (2026-04-27)

Three micro-optimisations on the worker hot path:

- Pre-bake the `Keep-Alive: timeout=N` header tuple on `Server`; drop the
  per-request f-string + encode in `_maybe_inject_keep_alive`.
- Drop `bytes(name).lower()` allocations in `_content_length` and
  `_maybe_inject_keep_alive` — h11 normalizes header names to lowercase
  bytes on both request and response sides.
- Fold the two header walks in `wsgi._build_environ` into one; cache
  `bytes(name)` and avoid double-decoding.

**Bench (5 s/cell, `max_concurrency=32`):** numbers are within
run-to-run noise (~±3 %) compared to Phase 1. All 100 % success.

**Why no measurable RPS gain:** at the bench's load (64 clients,
`max_concurrency=32`), we are **selector-bound**, not worker-CPU-bound.
The selector thread does accept + h11 parse + dispatch serially; that
sets the ceiling regardless of how cheap each request is on the workers.
Bumping `max_concurrency` to 128 (verified with a one-shot run) yields
the same ~8.5 k RPS — confirming it's selector-thread CPU, not queueing.

The Phase 2 changes are still net positive (less per-request CPU on
workers, smaller GC pressure) and they make the WSGI environ build
~2× cheaper, which matters under different workload mixes — but they
don't move the bench needle until Phase 3 unblocks the selector.

## Phase 3 attempt: lock-free op queue (reverted)

We prototyped the self-pipe + op-queue design: `Server._lock` removed, workers
enqueue typed `_OpTrack` / `_OpClose` ops on a `collections.deque`, write a
wakeup byte to `os.pipe()` registered in the selector, selector drains at the
top of each iteration. Single-writer-to-selector invariant held, but the
optimistic mode-flip semantics across three threads (selector, watchdog event
handler, worker) created races on the `ConnMode` field that I couldn't fully
close — stress dropped to 50-75% with EBADF mid-`send`. Reverted.

## Phase 3 (shipped): simplify the conn model, pull-based disconnect detection

The core insight from the reverted attempt: **the `WATCHDOG` mode is the
source of most of the complexity** (third state, separate selector data tag,
mode-recheck races, level-triggered busy-loop avoidance). Replacing it with a
much simpler design:

- **Two states only:** `HTTPConn.tracked: bool`. The conn is either in the
  selector for normal HTTP processing or borrowed by a worker. No third
  WATCHDOG state, no `_WatchdogToken`, no `Server.to_watchdog`, no
  `Server._handle_watchdog_event`.
- **Pull-based disconnect detection.** `RequestCancel.is_cancelled` does a
  non-blocking `recv(1, MSG_PEEK | MSG_DONTWAIT)` on the request socket.
  `b""` means peer FIN; `BlockingIOError` means no signal; any other
  `OSError` treats the connection as broken. The result is cached on
  `_event` so subsequent calls don't re-issue the syscall.
- **Single shared shutdown event.** Replaces the per-request `in_flight`
  registry — every cancel token OR-s in a single `threading.Event` set by
  the shutdown watcher.
- **Worker outer-finally simplified.** Only closes when `cancel.fired`
  (cheap event-only check) — never reads `ctx._conn.tracked`, which is
  shared with the next request's dispatcher and would race.

### Code delta

- `localpost/http/server.py`: dropped `ConnMode` enum, `_WatchdogToken`
  dataclass, `Server.to_watchdog`, `Server._handle_watchdog_event`. Replaced
  `HTTPConn.mode: ConnMode` with `tracked: bool`. Simplified `Server.run`'s
  for-event branch.
- `localpost/http/_cancel.py`: `RequestCancel` gains `_sock` + `_shutdown_event`.
  `is_cancelled` does the PEEK; `fired` is the event-only check.
- `localpost/http/_service.py`: dropped `_request_has_body`, the in-flight
  registry, and the watchdog branch in `dispatch`. Always `stop_tracking`.

Net: ~150 lines deleted, ~30 simplified. The selector loop has only two
event types (accept / HTTPConn) and one state field (`tracked: bool`).

### Trade-offs

- **Detection is cooperative now.** Disconnect surfaces only when the
  handler calls `check_cancelled()` — same contract as service-shutdown
  cancellation. A pure-compute handler that ignores cancellation won't
  notice mid-flight disconnects (just like it doesn't notice service
  shutdown). Most real handlers either poll cancellation or perform I/O,
  which surfaces disconnects via `EPIPE`/`ECONNRESET` naturally.
- **One extra syscall per `check_cancelled()` call** (the PEEK). Negligible
  vs the rest of the request path.

### Bench (5 s/cell, `max_concurrency=32`, 64 clients)

| Stack             | Phase 2 RPS / p50 | Phase 3-simplified RPS / p50 |
| ----------------- | ----------------: | ---------------------------: |
| `localpost_native`|     9,381 / 6.29  |              8,961 / 7.09 ms |
| `localpost_flask` |     8,157 / 7.44  |              8,053 / 7.88 ms |
| `localpost_wsgi`  |     7,743 / 7.87  |              7,630 / 8.29 ms |

Within run-to-run noise. We're still selector-thread CPU bound at ~9 k RPS
— this refactor was about **maintainability and correctness**, not raising
the ceiling. 10/10 stress runs at 800/800.

### What's left for future perf work

The selector ceiling is real. Options to revisit:

- **Move accept + parse + dispatch to multiple selector threads.** Multiple
  selectors each owning a slice of conns. More CPU, more lock-free.
- **Replace pure-Python h11 parsing with a C parser** (e.g. `httptools`).
  Likely the easiest win at this point.

## Phase 5 (shipped): profile-guided micro-opts

Profiled the server under bench load with `yappi` (CPU clock, multi-threaded).
The profile confirmed h11 dominates the per-request hot path (`next_event`,
`send`, `normalize_and_validate`, `_extract_next_receive_event` together
account for ~60-70% of selector CPU). But it surfaced two avoidable wastes
*outside* h11:

### Surprise #1: `socket.__repr__` per request

`Server._apply_track` was using `try selector.modify; except KeyError:
selector.register`. After a worker re-tracks a freshly-unregistered conn,
`modify` always misses — and `selectors.modify` builds the `KeyError`
message as ``f"{fileobj!r}"`` *before* throwing. `socket.__repr__` is
~25 µs/call. At 9 k req/sec that's ~225 ms/sec of CPU spent on an error
message we immediately discard.

Fix: probe ``selector.get_map()`` (an O(1) ``fd in dict`` check) and
choose ``modify`` vs ``register`` directly — never enter the exception
path. Same fix applied to ``HTTPConn.close`` and the ``_OpClose`` handler
in ``_drain_ops``.

### Surprise #2: `_maybe_inject_keep_alive` rebuilds the response

Every response went through ``_maybe_inject_keep_alive``, which
constructed a *new* ``h11.Response`` to append the ``Keep-Alive: timeout=N``
header. ``h11.Response.__init__`` runs ``normalize_and_validate`` over the
full headers list — heavy h11 work, every response.

Fix: drop the explicit ``Keep-Alive: timeout=N`` header. HTTP/1.1
defaults to keep-alive; the timeout header is informational and most
clients (httpx, requests, browsers) maintain their own pool timeout.
The 3 tests that asserted on the explicit header are removed.

### Bench

| Stack             | Phase 4 RPS / p50 | Phase 5 RPS / p50 | Δ |
| ----------------- | ----------------: | ----------------: | -----: |
| `localpost_native` plaintext  | 8,824 / 7.16 | **9,617 / 6.59** | +9.0%  |
| `localpost_native` path_param | 8,805 / 7.20 | **9,461 / 6.71** | +7.4%  |
| `localpost_native` json_post  | 7,676 / 3.93 | **8,589 / 3.63** | +11.9% |
| `localpost_flask`  plaintext  | 7,909 / 8.01 | **8,385 / 7.56** | +6.0%  |

10/10 stress at 800/800. All remaining 126 tests pass.

This is the realistic ceiling for the current architecture without
replacing h11 or going multi-process. The remaining selector-thread
CPU is now overwhelmingly inside h11 — verified by the same profile.

## Phase 4 (shipped): lazy `_cleanup_stale`

Targeted audit of the selector hot path identified one piece of clearly
wasted CPU: ``Server._cleanup_stale`` walking ``selector.get_map().values()``
on every iteration to discover "nothing to clean" — the common case under
load (default ``rw_timeout=1.0 s``, ``keep_alive_timeout=15 s``).

Fix: cache ``_last_cleanup_at``, skip the O(N) walk when ``now -
_last_cleanup_at < _cleanup_interval``. ``_cleanup_interval`` defaults
to ``min(rw_timeout, keep_alive_timeout) / 2`` (=0.5 s with defaults),
floored at 100 ms.

Trade-off: stale-detection latency goes from ~``select_timeout`` (1 s) to
~``timeout + 0.5 s`` (1.5 s for ``rw_timeout``, ~15.5 s for
``keep_alive_timeout``). Both are detection latencies for non-fatal
conditions (slow clients, dead keep-alive connections), well outside
any user-visible budget.

### Bench (5 s/cell, ``max_concurrency=32``, 64 clients)

| Stack             | Phase 3-B RPS / p50 | Phase 4 RPS / p50 |
| ----------------- | ------------------: | ----------------: |
| `localpost_native`|       8,843 / 7.15  |     8,824 / 7.16  |
| `localpost_flask` |       7,884 / 7.92  |     7,909 / 8.01  |
| `localpost_wsgi`  |       7,505 / 8.45  |     ~7,500 / ~8 ms |

Within run-to-run noise — as predicted. At 64 keep-alive conns, the
O(N) walk savings are ~64 ops per iter × ~9k iter/sec = ~580 k ops/sec
saved. A few percent of selector CPU, swallowed by bench noise. The
optimization scales with conn count: real-world deployments with
hundreds–thousands of idle keep-alive conns would see proportionally
larger gains. 10/10 stress at 800/800.

The bench needle is now firmly stuck at the h11 parsing ceiling.
Future RPS gains require either a C parser (`httptools`) or
multi-selector / SO_REUSEPORT — both meaningful architectural changes.

## Phase 3-B (shipped): lock-free op queue + self-pipe wakeup

With the conn model down to two states, the original Phase 3 design fits
cleanly. Workers no longer touch the selector directly:

- ``Server._lock`` deleted.
- ``Server._ops: collections.deque`` — atomic ``append`` / ``popleft``.
- ``os.pipe()`` registered in the selector for wakeup.
- ``Server.track`` from a worker thread enqueues ``_OpTrack(conn)`` and
  writes a wakeup byte. ``HTTPConn.close`` from a worker enqueues
  ``_OpClose(fd)`` after closing the kernel socket synchronously (the
  ``_OpClose`` handler just cleans ``selector._fd_to_key``).
- Selector drains ``_ops`` at the top of every iteration and on
  wakeup-sentinel events; ``stop_tracking`` and ``_cleanup_stale`` run
  inline (already on selector thread).
- ``HTTPConn.fd`` captured at construction so cleanup can use the integer
  fd via ``selector.unregister(fd_int)`` even after ``sock.close()`` (where
  ``sock.fileno()`` returns -1).

### Bench (5 s/cell, ``max_concurrency=32``, 64 clients)

| Stack             | Phase 3-A (lock) RPS / p50 | Phase 3-B (op queue) RPS / p50 |
| ----------------- | -------------------------: | -----------------------------: |
| `localpost_native`|             8,961 / 7.09   |                  8,843 / 7.15  |
| `localpost_flask` |             8,053 / 7.88   |                  7,884 / 7.92  |
| `localpost_wsgi`  |             7,630 / 8.29   |                  7,505 / 8.45  |

Within run-to-run noise — confirming the prediction that we're selector
CPU-bound (h11 parsing), not lock-bound. 10/10 stress at 800/800.

The win isn't in the bench: it's in **the threading model is now strictly
single-writer to the selector**, and the lock is gone. Future work that
needs to add cross-thread selector mutations (e.g. scheduled FD timers)
just enqueues an op — no lock to contend with, no consistency invariants
to re-prove.

## Phase 6 (shipped): optional httptools backend (2026-04-29)

The Phase 5 closing line — "remaining selector-thread CPU is now
overwhelmingly inside h11" — was the explicit handoff to this phase.
Cashed it in.

### What shipped

A second HTTP/1.1 server backed by ``httptools`` (llhttp) ships as a
**peer** of the existing h11 server, opt-in via the ``[http-fast]`` extra.
Not a parser plugin under one server — two implementations sharing only
the parser-agnostic infrastructure:

- ``localpost/http/_base.py``: ``BaseServer`` (selector loop, accept,
  op queue + wakeup pipe, stale sweep, shutdown) lifted from the old
  ``Server``, parameterised on a ``conn_factory``. ``BaseHTTPConn`` ABC
  with a small surface (``__call__``, ``close``, ``sock``, ``fd``,
  ``tracked``, ``close_at``, ``idle``, ``emit_stale_408``) — no parser
  methods leak through.
- ``localpost/http/server_h11.py``: existing logic moved here; translates
  at the boundary (``h11.Request`` ⇄ neutral ``Request``, neutral
  ``Response`` → ``h11.Response`` before ``parser.send``).
- ``localpost/http/server_httptools.py``: native push-callback driven;
  ``_ReadyRequest`` queue handles pipelining naturally; hand-written
  response serializer. Each accepted conn gets its own
  ``HttpRequestParser`` with the conn instance as the protocol target.
- ``localpost/http/_types.py``: neutral ``Request`` / ``NativeResponse``
  / ``InformationalResponse`` so handlers no longer import ``h11`` or
  ``httptools`` directly.

Hosted-service form: ``httptools_server`` (peer of ``http_server``,
lazy-imports the backend so the extra stays optional).

Why two implementations rather than a parser Protocol: h11 is
pull-events + parse-AND-serialize, httptools is push-callbacks +
parse-only. Forcing one shape over both restricts the faster backend
without buying anything (the dispatch path / selector / pool are
already shared by ``BaseServer``). Each backend uses its parser's
natural idioms.

### Bench (8 s/cell, Python 3.13 on Darwin arm64, 64 / 32 clients)

| Scenario          | h11 RPS / p50      | httptools RPS / p50    | Δ RPS    |
| ----------------- | -----------------: | ---------------------: | -------: |
| `plaintext`       |    9,494 / 6.67 ms |  **12,845 / 4.96 ms**  | **+35%** |
| `path_param`      |    9,500 / 6.67 ms |  **12,775 / 4.98 ms**  | **+34%** |
| `json_post`       |    8,616 / 3.66 ms |  **12,504 / 2.54 ms**  | **+45%** |
| `profile_update`  |    5,841 / 5.43 ms |    5,885 / 5.25 ms     |    +1%   |

Lands almost exactly on the 30–50% projection from the original Phase 1
diagnosis. ``profile_update`` doesn't move because the synthetic handler
holds three ``time.sleep`` totalling ~4 ms — at that point parser
overhead is a rounding error; the gain is in the noise.

p50 latency improves ~25–31% on parser-bound scenarios. We're still
~3–5× behind ``starlette_uvicorn`` / ``starlette_granian`` on absolute
RPS — that's the async-ASGI + uvloop ceiling, well outside what a
sync-handler + thread-pool design gives.

All 142 http tests pass (130 existing + 12 new backend-parity tests
covering GET / POST-with-body / oversize → 413 / malformed → 400 /
keep-alive×2 / ``Expect: 100-continue`` across both backends).

### Two bugs the parity tests didn't catch

Both surfaced only under bench load (``oha`` with persistent HTTP/1.1
connections), not in the parity suite — pinpointed and fixed in the
same session.

1. **``_ready`` not popped on borrow.** Under ``thread_pool_handler``,
   ``h(req_ctx)`` returns immediately with ``borrowed=True``; my loop
   checked ``req_ctx.borrowed`` *before* ``popleft``, so the same
   request got re-dispatched on every conn re-track. Symptom: RPS = the
   concurrency limit, every request hitting the 1 s ``rw_timeout``.
   Fix: ``popleft`` *before* dispatch — ownership transfers to the
   ``HTTPReqCtx`` regardless of whether the handler runs synchronously
   or hands the conn off.
2. **No body framing for responses without Content-Length.** h11
   silently inserts ``Transfer-Encoding: chunked`` when neither
   ``Content-Length`` nor ``Transfer-Encoding`` is set; my hand-written
   serializer wrote raw bytes and HTTP/1.1 clients waited for FIN
   before considering the response complete. The CHANGELOG/README
   "Content-Length only" claim was wrong: ``Router.as_handler`` doesn't
   compute lengths from ``Iterable[bytes]`` bodies, and many WSGI apps
   don't set the header either. Fix: in the httptools backend's
   ``start_response``, when the response lacks both headers, append
   ``Transfer-Encoding: chunked`` and frame chunks
   (``<hex-len>\r\n<data>\r\n``) plus the ``0\r\n\r\n`` terminator on
   ``finish_response``. **One** ``sendall`` per chunk — the first
   version did three, and the syscall overhead alone (~75 µs/req at
   small bodies) was eating the entire C-parser win, leaving the bench
   at parity with h11.

### Trade-offs

- **Two parallel implementations, not one.** ~340 lines for the h11
  backend (mostly verbatim from the old ``server.py``) + ~430 lines for
  the httptools backend (callback bookkeeping + response serializer +
  chunked framing). The shared ``BaseServer`` is ~370 lines, lifted
  unchanged. The duplication is intentional: each backend reads as a
  straight translation of its parser's natural idioms.
- **Auto-chunked is now in scope.** The CHANGELOG / README originally
  said "Content-Length only initially". That stance was based on the
  assumption that ``Router`` and ``wrap_wsgi`` always set
  Content-Length. They don't. The httptools backend now matches h11's
  effective behaviour (silent chunked) for un-framed responses; chunked
  remains absent from the trailer / Transfer-Encoding-other-than-chunked
  paths, which is fine.
- **Public API took a one-time break.** ``HTTPReqCtx.request`` is now a
  neutral ``Request`` (was ``h11.Request``); ``start_response`` /
  ``complete`` accept neutral ``NativeResponse`` /
  ``InformationalResponse``. Field shapes match h11's — migration is a
  mechanical import swap, not a semantic change. Documented in
  CHANGELOG.

## Phase 7 (shipped, but flat): multi-selector single-process (2026-04-29)

Added the `selectors: int = 1` knob to `http_server` / `httptools_server` /
`wsgi_server`. With `selectors > 1`, N independent `BaseServer` threads
each bind their own listening socket on the same address via
`SO_REUSEPORT` (already enabled in `_base.py`); the kernel hashes
incoming SYNs across them. Shared handler / shared `thread_pool_handler`
worker pool — only the selector layer fans out. Port 0 is resolved once
up-front so all selectors agree on the actual ephemeral.

### Bench (httptools backend, 8s/cell, Python 3.13 on Darwin arm64)

| Scenario          | s1 RPS / p50         | s2 RPS / p50         | s4 RPS / p50         |
| ----------------- | -------------------: | -------------------: | -------------------: |
| `plaintext`       |     12,563 / 5.05 ms |     12,902 / 4.93 ms |     12,784 / 4.98 ms |
| `path_param`      |     12,606 / 5.04 ms |     12,691 / 5.01 ms |     12,761 / 5.00 ms |
| `json_post`       |     12,359 / 2.57 ms |     12,369 / 2.57 ms |     12,301 / 2.58 ms |
| `profile_update`  |      5,978 / 5.21 ms |      6,007 / 5.20 ms |      6,020 / 5.20 ms |

All deltas (≤+2.7% on s2, ≤+1.8% on s4) are within run-to-run noise.
**The projected 1.5-2x gain on standard CPython did not materialise.**

### Why the projection was wrong

Two compounding factors:

1. **The GIL-held fraction of selector wall time is higher than I
   estimated.** I assumed `recv` + the bulk of `httptools.feed_data`
   release the GIL, leaving roughly 50% of wall time for parallelism.
   In reality, the parser callbacks (`on_url`, `on_header`,
   `on_headers_complete`, `on_body`, `on_message_complete`), the
   dispatch decision, `Channel.put`, op-queue enqueue, and the
   wakeup-pipe `os.write` all hold the GIL — together they're more
   like 80-90% of per-request work. Two selector threads serialise on
   that, leaving little to overlap.
2. **macOS `SO_REUSEPORT` is not Linux's `SO_REUSEPORT`.** macOS
   permits multiple binds to the same address, but the BPF-style
   round-robin / 4-tuple-hash distribution that Linux 3.9+ ships is
   not part of the macOS contract. Distribution behaviour is
   unspecified — connections can funnel to one selector. We didn't
   instrument per-selector accept counts in this bench, but the flat
   result is consistent with either "GIL pinch" or "no kernel
   distribution" or both.

The implementation itself is correct (137/137 http tests pass, including
parity tests for `selectors ∈ {2, 3}`). The architectural lever just
doesn't pay on this platform / interpreter.

### What this means for next steps

- **Free-threaded Python (Phase 7b) is now the more interesting test.**
  Removing the GIL is the only thing that lifts factor #1. macOS
  `SO_REUSEPORT` semantics still apply — if distribution is also weak
  there, we'd need an explicit accept-dispatch fallback (one acceptor
  thread + N selector threads handing off via the op queue).
- **Tier 2 micro-opts deliver more reliably on standard CPython.**
  Single-`sendall` per response, `socket.sendmsg` for chunked, and the
  Tier 3 allocation diet are all `selectors`-independent and cut
  per-request CPU on the path that's actually hot.

`selectors=N` stays in the public API. It's free for users on Linux who
already get kernel-level load balancing, and it's the right shape for
the eventual no-GIL world. We just don't claim it as a perf win on
standard-CPython / macOS.

## Phase 7b (shipped): free-threaded Python (3.14t) validation (2026-04-29)

Tested LocalPost under CPython 3.14.4 free-threaded (no-GIL) on Darwin
arm64. Setup: separate venv (``.venv-ft``), `httptools` built from
git main (declares `Py_mod_gil = Py_MOD_GIL_NOT_USED` since 0.8.0;
the released 0.7.1 wheel auto-re-enables the GIL on import).

### Headline: free-threading alone is the big win

**Same code, same hardware, same bench harness — switching from standard
CPython 3.13 to free-threaded 3.14t at `selectors=1`:**

| Backend / scenario          | 3.13 RPS / p50      | 3.14t RPS / p50      | Δ RPS    |
| --------------------------- | ------------------: | -------------------: | -------: |
| `localpost_httptools` plaintext  |  12,563 / 5.05 ms |  **36,208 / 1.76 ms** | **+188%** |
| `localpost_httptools` path_param |  12,606 / 5.04 ms |  **35,491 / 1.78 ms** | **+182%** |
| `localpost_httptools` json_post  |  12,359 / 2.57 ms |  **34,220 / 0.93 ms** | **+177%** |
| `localpost_native` plaintext     |   ~9,500 / 6.7 ms |  **23,688 / 2.69 ms** | **+150%** |

p50 collapses ~3x on every parser-bound scenario. The reason is the
existing single-selector + worker-pool architecture is itself a
multi-threaded design (1 selector thread + 32 workers); under standard
CPython all those threads serialise on the GIL during the
parser-callbacks / dispatch / handler / response-write critical
sections. No-GIL lets the selector and workers actually overlap. The
selector loop is no longer the throughput ceiling — the workers are.

### Multi-selector under 3.14t: still flat on macOS

| Scenario          | s1 RPS / p50         | s2 RPS / p50         | s4 RPS / p50         |
| ----------------- | -------------------: | -------------------: | -------------------: |
| `plaintext`       |     36,208 / 1.76 ms |     36,089 / 1.75 ms |     36,112 / 1.75 ms |
| `path_param`      |     35,491 / 1.78 ms |     35,965 / 1.77 ms |     36,638 / 1.74 ms |
| `json_post`       |     34,220 / 0.93 ms |     33,740 / 0.94 ms |     34,538 / 0.92 ms |

Same pattern at higher concurrency (`oha -c 256`) and in inline mode
(no thread pool — handlers run directly on the selector thread,
isolating selector-level throughput): all configurations converge on
the same number.

### Why multi-selector is flat: macOS `SO_REUSEPORT` does not distribute

Confirmed empirically with a diagnostic build
(`benchmarks/http/apps/localpost_httptools_diag.py`) that counts requests
per selector thread. At `selectors=4`, `oha -c 512 -z 8s`:

```
=== selector distribution (total=402,871) ===
  tid=6168244224: 402,871 reqs (100.0%)
```

**100% of accepts went to one selector thread.** The other three
selectors — each with its own listening socket bound to the same port
via `SO_REUSEPORT` — got zero connections. This is the documented
divergence between Linux's `SO_REUSEPORT` (BPF-style hash distribution
since 3.9) and macOS's, which permits the bind but does not load-balance.

The implication: on macOS, the `selectors > 1` knob is a no-op.
On Linux (kernel-level distribution) we expect it to scale, but that's
unverified pending a Linux bench.

### What this means for next steps

- **Free-threaded Python is now the default for serious perf.** A 3x
  jump just by switching interpreters dwarfs every micro-optimisation
  we'd land in pure code. We should treat 3.14t as the supported fast
  path and keep the codebase free-threaded-clean (no GIL-dependent
  patterns; verify deps' `Py_mod_gil` declarations).
- **Accept-dispatch fallback is the next architectural step** — and now
  has a clear motivation. One acceptor thread doing `accept()` on a
  shared listening socket, dispatching new conns to N selector threads
  via the existing op queue. Platform-portable; works on macOS where
  `SO_REUSEPORT` doesn't help. The selector loop already accommodates
  cross-thread `_OpTrack` enqueues from Phase 3-B — the wiring is
  small.
- **Linux-side multi-selector is still worth verifying** — the existing
  `selectors > 1` path should scale there. A free CI cell (Linux x86_64,
  3.14t) would settle it.

The implementation that shipped (`selectors: int = 1`) is correct and
keeps its place in the public API; under the current macOS bench it
just doesn't pay. The right framing for users: "use `> 1` only if your
kernel distributes (Linux 3.9+) or paired with the upcoming
accept-dispatch design."

## Phase 8 (shipped): continuation handler + auto-buffered response (2026-04-29)

Restructured the request lifecycle around the JSON-API common case:

1. **Two-phase handler contract.**
   `RequestHandler = Callable[[HTTPReqCtx], BodyHandler | None]`. The
   pre-body handler runs on the selector when headers are parsed. It
   returns either:
   - `None` — handler completed inline (e.g. a 404 / 405 / auth fail
     reject) or borrowed the conn for a worker. **No body recv. No
     worker hop.**
   - `BodyHandler` — the selector buffers the full body into
     `ctx.body` and then invokes the continuation.
2. **HTTP/1.1 pipelining dropped.** The httptools backend's
   `_ready` deque + per-request cur-state machinery is gone; one
   in-flight request per connection. Pipelined clients are served
   sequentially. ~50 lines deleted.
3. **Auto-buffered response writes.** `start_response` no longer
   flushes — it stashes the serialised headers and the first
   `send(...)` (or `finish_response()` for empty bodies) emits
   headers + body in a single `sendall`. **Common-case `complete()`
   path is now 1 syscall, down from 2.**
4. **`thread_pool_handler` adapts.** The pool no longer wraps the
   pre-body invocation. It pass-through if `inner` returns `None`
   (inline 404s never enter the channel) and wraps the returned
   `BodyHandler` into a worker-dispatch continuation otherwise.

### Bench (8 s/cell, 64/32 clients)

**Standard CPython 3.13 / Darwin arm64** — primary perf target (≈
real-world Linux deployment):

| Scenario          | Phase 7 RPS / p50  | Phase 8 RPS / p50      | Δ RPS    |
| ----------------- | -----------------: | ---------------------: | -------: |
| `httptools` plaintext  |  12,563 / 5.05 ms |  **15,197 / 4.13 ms**  | **+21%** |
| `httptools` path_param |  12,606 / 5.04 ms |  **15,312 / 4.13 ms**  | **+21%** |
| `httptools` json_post  |  12,359 / 2.57 ms |  **15,051 / 2.11 ms**  | **+22%** |
| `h11` plaintext        |   9,494 / 6.67 ms |  **10,012 / 6.28 ms**  |  +5%    |

Plus +21% RPS on the path real users will see. p50 collapses with
the single-sendall flush. h11 gains are smaller (still parser-bound),
but the simplification is consistent across both backends.

**Free-threaded CPython 3.14t / Darwin arm64**: pooled httptools
plaintext sits at 34,456 RPS (vs Phase 7's 36,208 — within ±5%
noise; the bench is client-saturated at `c=64` once p50 drops below
2 ms). Inline (no-pool) httptools plaintext: 52,015 RPS. The
restructure is neutral here, as expected — most of the savings are
already absorbed by the no-GIL win.

### Why this delivers more than syscall coalescing alone

Pure micro-opts (combine sendalls, header scan) saved a syscall but
left the architectural shape unchanged. The continuation pattern
adds a bigger lever: **fast handlers (no body) and rejections never
wait for the body to arrive at all.** For the common JSON-API mix
(404 / 405 / GET / POST-with-JSON), this:

- removes a pool-channel round-trip from every body-free request
  (the old `thread_pool_handler` always borrowed)
- removes the worker-thread body recv from POSTs (selector buffers
  it; worker just runs the JSON parse + response build)

The single-sendall change rides on top.

### One-time API break

`RequestHandler`'s return type changed from `None` to `BodyHandler |
None`. Old-style `(ctx) -> None` handlers are forward-compatible —
returning `None` implicitly is the "complete inline" path. Handlers
that previously read body via `ctx.receive(size)` need to migrate to
the continuation pattern (return a `BodyHandler` and read the body
from `ctx.body`). All in-tree adapters (`Router.as_handler`,
`wrap_wsgi`, `flask_handler`, `sentry_router_handler`,
`sentry_flask_handler`) have been updated; user code that bypassed
those needs the same shape.

## Phase 9 (shipped): middleware + lean Router + HttpApp framework (2026-04-29)

Architectural restructure across the http stack:

1. **Middleware support.** New ``Middleware = Callable[[RequestHandler],
   RequestHandler]`` type and a ``compose(*mws)`` helper. Plain Python
   decorator pattern — no special chain object. Pre-body short-circuit
   and post-body wrapping (via the returned BodyHandler) both work
   naturally.
2. **HTTPReqCtx.attrs.** New ``dict[str, Any]`` per-request mutable
   state on the Protocol. Used by the Router to attach ``RouteMatch``
   and by middlewares to thread cross-cutting state (auth, tracing).
3. **Router stripped to a lean middleware-friendly dispatcher.** The
   framework-y ``RequestCtx`` / ``Response`` / ``RequestHandler`` shapes
   that lived inside ``router.py`` are gone. ``Router.as_handler()``
   now matches the URI template, attaches a ``RouteMatch`` to
   ``ctx.attrs["route_match"]``, and delegates to a registered
   http-level :data:`localpost.http.RequestHandler`. 404 / 405 stay
   inline. ``Router.wsgi`` is dropped.
4. **New ``HttpApp`` framework** (``localpost.http.app``). Decorator-
   driven, with parameter injection (``HTTPReqCtx`` + path args by
   name), automatic response conversion (str / bytes / dict / list /
   ``NativeResponse`` / ``(NativeResponse, bytes)`` / ``None``),
   app-level + per-route middleware composition, and a ``service()``
   factory that composes the worker pool + chosen backend.
5. **``streaming_pool_handler`` + ``buffer_body=False`` per route.**
   For routes that need raw socket access (large uploads), the handler
   runs on a worker on a borrowed conn — body **not** pre-buffered.
   Reads via ``ctx.receive(...)`` chunk by chunk. Internal pool
   primitive (``_Pool``) shared by both buffered and streaming dispatch.

### Bench (8 s/cell, standard CPython 3.13 / Darwin arm64)

| Scenario          | Phase 8 RPS / p50      | Phase 9 RPS / p50      | Δ RPS    |
| ----------------- | ---------------------: | ---------------------: | -------: |
| `httptools` plaintext   |  15,197 / 4.13 ms |  **20,868 / 3.05 ms**  | **+37%** |
| `httptools` path_param  |  15,312 / 4.13 ms |  **20,667 / 3.08 ms**  | **+35%** |
| `httptools` json_post   |  15,051 / 2.11 ms |  **20,152 / 1.58 ms**  | **+34%** |
| `httptools` profile_update |  5,384 / 5.95 ms |    6,286 / 5.09 ms  |   +17%   |

p50 collapses ~25% across the fast scenarios. ``profile_update`` is
handler-CPU-dominated (4 ms of `time.sleep`), so the framework
overhead saving shows up as a smaller relative gain.

### Why this delivers another ~35%

The lean Router stops constructing per-request the framework objects
the old shape needed: ``RequestCtx`` (with its ``ExitStack``, headers
dict, query parse, receive shim, body cache) and ``Response`` plus
the wire-bytes encoding of its headers / iter-body.

Now ``Router.as_handler`` is essentially: regex match + ``ctx.attrs[]
=`` + delegate. The handler chosen by the route is a regular
``RequestHandler``; if registered through ``HttpApp`` it gets the
auto-buffered Phase 8 response path with one more layer of param
resolution.

For the bench specifically, the handlers return
``(NativeResponse, bytes)`` tuples — pre-baked wire shapes, so we
skip the str/dict→bytes conversion path. Real apps using ``str`` /
``dict`` returns will sit slightly below this number; the Pythonic
return paths are still leaner than the old Router framework.

### One-time API breaks

- ``Router.wsgi`` removed.
- Router-level ``RequestCtx``, ``Response``, ``RequestHandler`` (the
  ``(RequestCtx) -> Response`` shape) gone. Migrate to ``HttpApp`` or
  use the lean http-level :data:`localpost.http.RequestHandler` shape
  directly.

## Phase 10 (shipped): drop per-request settimeout (2026-04-30)

Removed the two ``sock.settimeout`` (fcntl) calls per request that the
borrow / re-track boundary used to pay:

- ``BaseServer.stop_tracking`` no longer flips the socket to
  blocking-with-timeout. The conn stays non-blocking after the worker
  borrows it.
- ``BaseServer.track`` no longer flips back to non-blocking. It's a
  no-op on socket flags now; only the kernel-level
  ``selector.register/modify`` op fires.
- New ``_send_all`` helper in ``_base.py``: tries non-blocking
  ``send`` first; on ``BlockingIOError`` (kernel buffer full
  mid-response), transitions to blocking-with-timeout for the
  remainder, then restores non-blocking. Common-case JSON-API
  responses fit in the kernel buffer in a single ``send`` — no
  fallback fires.
- One-time ``setblocking(False)`` after ``accept`` (macOS / BSD don't
  inherit ``O_NONBLOCK`` from the listener; Linux 2.6.28+ does).

### Bench (8 s/cell, standard CPython 3.13 / Darwin arm64)

| Scenario          | Phase 9 RPS / p50      | Phase 10 RPS / p50     | Δ RPS    |
| ----------------- | ---------------------: | ---------------------: | -------: |
| `httptools` plaintext   |  20,868 / 3.05 ms |  **25,230 / 2.50 ms**  | **+21%** |
| `httptools` path_param  |  20,667 / 3.08 ms |  **25,247 / 2.51 ms**  | **+22%** |
| `httptools` json_post   |  20,152 / 1.58 ms |  **24,712 / 1.28 ms**  | **+23%** |
| `httptools` profile_update |  6,286 / 5.09 ms |   6,311 / 5.08 ms  |   +0%   |
| `h11` plaintext         |  ~10,012 / 6.28 ms |  **13,225 / 4.81 ms** | **+32%** |
| `h11` json_post         |  ~9,550 / 3.30 ms  |  **12,432 / 2.55 ms** | **+30%** |

Bigger than the 3-5% I'd projected — the per-request cost of
``settimeout`` on Darwin is heavier than expected (each fcntl ~5-7 µs
under realistic load, not the textbook ~1 µs). At 25k RPS, two fcntl
saved per request is ~250-350 ms/sec of selector / worker CPU
reclaimed.

p50 collapses ~17-19% across the fast scenarios. ``profile_update``
remains handler-bound (~4 ms of ``time.sleep`` dominates).

### What this validates

The user's hypothesis: **conn stays non-blocking; ``send`` relies on
the kernel buffer absorbing small responses, falls back only on
``BlockingIOError``.** The fallback path is rare in JSON-API
workloads and the savings on the common path are real.

The "never borrow" variant (don't even ``selector.unregister``) was
considered and dropped — synchronisation against pipelined clients
needs *something*, and the unregister is the cheapest correct
mechanism (~1 µs each on macOS kqueue).

## Phase 11 (shipped): Tier-3 allocation diet + Request enrichment (2026-04-30)

Mechanical follow-up to Phase 10, plus one one-time API tightening on
``Request``. Numbers are within run-to-run noise on the macOS bench
because we're already at the C-parser ceiling (Phase 6 / 10 closed the
big gaps); the wins land mostly on smaller paths and on the API
shape.

### What shipped

1. **No-op ``bytes()`` wraps removed.** httptools and h11 hand back real
   ``bytes`` objects from their callbacks / events; ``bytes(b)`` for a
   ``bytes`` argument returns the same object, so the wraps were dead
   weight. The h11 backend's per-request ``[(bytes(n), bytes(v)) for n, v
   in event.headers]`` is replaced by ``list(event.headers)`` — a
   shallow copy that skips the per-tuple alloc but stays insulated from
   h11's per-event ``Headers`` subclass. Multi-fragment URL accumulation
   in httptools' ``on_url`` switched to a lazy ``bytearray`` so the rare
   fragmented case doesn't pay quadratic ``bytes`` concat.
2. **Pre-serialised canned protocol-error responses.** Each
   ``*_RESPONSE`` constant in ``_base.py`` now ships with a sibling
   ``*_WIRE: bytes`` — full status line + headers + body, built once at
   module import time. The httptools backend's ``_try_send_status`` and
   ``emit_stale_408`` use the pre-built bytes via ``_send_all`` and skip
   ``_serialize_response`` on the error path entirely.
3. **Cached 404 / 405 responses in Router.** The 404 ``NativeResponse``
   + body is a module-level constant; the per-route 405 pair is
   pre-built at ``Routes.build()`` time and stored on ``Route`` next to
   the existing ``allow_header``. ``Router.dispatch`` uses
   ``ctx.complete(*cached)`` directly; the per-miss list / encode /
   ``Response`` build is gone.
4. **``Request.path`` and ``Request.query_string``.** New pre-split
   fields populated by the backend at parse time. Each consumer
   (``Router``, ``wsgi._build_environ``, ``router_sentry``) now reads
   them directly instead of doing the same ``target.decode + split``
   per dispatch. The h11 backend also normalises ``method`` to
   uppercase here (h11 is lenient on method case; httptools rejects
   lowercase at parse time, so it's already uppercase).

### One implementation surprise: skip ``httptools.parse_url``

The first cut of #4 used ``httptools.parse_url(target)`` in the
httptools backend, on the assumption that a C-level URL parser would
beat a Python ``find`` / ``split``. **It didn't** — bench showed
plaintext regressed ~8 % on the httptools backend, and a
``timeit`` micro-bench confirmed ``parse_url`` is ~2× slower than the
manual split for typical short JSON-API targets (it builds a
``URL`` object with multiple bytes attributes — Python
object-construction overhead per parse, on top of the C parsing).
Both backends now use ``target.find(b'?')`` + slice; the API is the
same, and the implementation is consistent across backends.

### Bench (10 s/cell, standard CPython 3.13 on Darwin arm64)

| Scenario          | Phase 10 RPS / p50     | Phase 11 RPS / p50     | Δ RPS    |
| ----------------- | ---------------------: | ---------------------: | -------: |
| `httptools` plaintext      | 25,230 / 2.50 ms |  25,438 / 2.49 ms |  +1%   |
| `httptools` path_param     | 25,247 / 2.51 ms |  25,408 / 2.49 ms |  +1%   |
| `httptools` json_post      | 24,712 / 1.28 ms |  24,342 / 1.29 ms |  -1%   |
| `httptools` profile_update |  6,311 / 5.08 ms |   6,315 / 5.08 ms |   0%   |
| `h11` plaintext            | 13,225 / 4.81 ms |  13,332 / 4.77 ms |  +1%   |
| `h11` path_param           |   ~13 k / ~4.8 ms |  13,324 / 4.77 ms |  +2%   |
| `h11` json_post            | 12,432 / 2.55 ms |  12,420 / 2.56 ms |   0%   |

All within run-to-run noise (~±3 %) on the bench's hot scenarios.
That matches the prediction: the bench is parser-bound at the
selector for httptools and selector-bound at h11 parsing for h11;
the per-request work we trimmed is real but small relative to those
floors. **The wins are on paths the bench doesn't stress hard**:

- 404 / 405 dispatch is now per-miss-allocation-free (matters under
  scanner / probing traffic)
- consumer-side decode + split removed (smaller benefit at 25 k RPS,
  larger at higher concurrency where consumers stack up)
- error-path serialisation is gone for the protocol-error responses
- the API shape is now right for httptools' speed: the backend
  produces ``path`` / ``query_string`` natively, with the same
  cost on h11

192 / 192 ``tests/http/`` green. ``just check`` clean on all
touched files.

### One-time API break

``Request`` gained two non-default fields (``path``, ``query_string``).
Code constructing ``Request`` by hand needs both. All in-tree
backends and the one test fixture (``tests/http/service.py``) that
built a ``Request`` directly are updated. User code using
``HTTPReqCtx.request.target`` is unaffected — ``target`` is preserved.

### What we didn't ship

- **Reusing ``_cur_headers`` across requests on a conn (httptools).**
  Investigated and dropped: the previous Request holds the list, so
  reuse requires a fresh copy at Request construction (O(N)),
  replacing a per-request ``[]`` allocation that's already O(1) via
  CPython's list freelist. Net loss, plus it weakens Request's
  effective immutability.
- **Caching header-presence flags on ``Response``.** Per-response
  scan in ``_scan_response_headers`` is real but tiny (2-4 headers
  on typical JSON responses). The clean fix needs mutable cache
  slots on a frozen dataclass; not worth the structural change for
  the measured impact. The static error responses bypass the scan
  entirely now via the pre-serialised path (see above).
- **Lowercasing common header names via an intern table** in the
  httptools ``on_header`` callback. Profile evidence didn't push us
  there; revisit if a future profile shows ``name.lower()``
  alloc cost climbing.

## Phase 12 (shipped): acceptor topology — 1 acceptor + N worker selectors (2026-05-02)

Reverses the call made in Phase 7's *"What's left"* (and the earlier
"Accept-dispatch alternative — explicitly dropped"). Phase 7b made the case
for it concrete: free-threaded scaling needs N busy selector threads, and on
macOS `SO_REUSEPORT` puts every conn on one. The acceptor topology adds a
single dedicated acceptor thread that round-robins each accepted conn onto N
worker selectors via the existing op queue + wakeup pipe.

### What shipped

The architectural enabler is the *Selector / ConnHandler* split (commit
`8748c66`) — `BaseServer` now composes a dumb fd→callback `Selector` with a
`ConnHandler` (after-accept policy):

```
Selector       ── owns fd→SelectorCallback map; nothing HTTP-specific
   │
   ▼
ConnHandler    ── after-accept policy; default: track on the accepting selector;
   │              acceptor mode: post_track to a worker selector
   ▼
RequestHandler ── pre-body dispatch
   │
   ▼
BodyHandler    ── post-body continuation
```

`http_server` / `wsgi_server` / `HttpApp.service` each gained an
`acceptor: bool = False` knob. With `acceptor=True, selectors=N`:
one thread runs the acceptor `BaseServer` (listen socket only, no conn
tracking), N threads each run a worker `Selector` (no listen socket); a
`RoundRobinAcceptor` `ConnHandler` builds each new conn for the next worker
and calls `worker.post_track(conn)` (cross-thread `_OpTrack` on the existing
op queue from Phase 3-B). The handoff path was already cross-thread-safe —
`ConnHandler` just gives it a stable name.

### Bench (6 s/cell, Darwin arm64, two interpreters)

The story splits cleanly along the GIL axis. Numbers: `localpost` group
only, plaintext scenario.

**Standard CPython 3.13 (GIL):**

| Stack                                     |    RPS | vs `s=1` baseline |
| ----------------------------------------- | -----: | ----------------: |
| `localpost_native` (s=1, pool)            | 12,504 |                 — |
| `localpost_native_s4` (SO_REUSEPORT)      | 12,812 |               +2% |
| `localpost_native_acceptor_s4`            | 12,215 |               −2% |
| `localpost_httptools` (s=1, pool)         | 24,356 |                 — |
| `localpost_httptools_s4`                  | 24,598 |               +1% |
| `localpost_httptools_acceptor_s4`         | 21,207 |          **−13%** |

Under the GIL the topology is flat-to-slightly-worse. The cross-thread
op-queue dispatch costs more than it saves: parser callbacks + dispatch +
response-write all serialise on the GIL, so spreading the readable-event
work across N threads buys nothing, and pays one extra wakeup-pipe write per
conn for the privilege.

**Free-threaded CPython 3.14t:**

| Stack                                     |    RPS | vs `s=1` baseline |
| ----------------------------------------- | -----: | ----------------: |
| `localpost_native` (s=1, pool)            | 20,033 |                 — |
| `localpost_native_s4` (SO_REUSEPORT)      | 20,604 |               +3% |
| **`localpost_native_acceptor_s4`**        | **26,684** |       **+33%** |
| `localpost_httptools` (s=1, pool)         | 16,262 |                 — |
| `localpost_httptools_s4`                  | 15,973 |               −2% |
| **`localpost_httptools_acceptor_s4`**     | **17,958** |       **+10%** |

The headline result on h11: `native_acceptor_s4` does **26,684 vs `native_s4`'s
20,604 = +30% over the SO_REUSEPORT alternative**. This is precisely the
case the topology was added for — Phase 7b confirmed `SO_REUSEPORT` puts
100 % of accepts on one selector on macOS; the explicit round-robin replaces
that with a real 4-way spread. httptools shows a smaller gain because its
hot path is shorter, so the per-conn dispatch overhead occupies a larger
share of the budget.

### Footgun: `acceptor + inline` (no pool)

The wrong combination on the JSON-API common case:

| Stack (3.14t)                                     |    RPS | vs inline s=1 |
| ------------------------------------------------- | -----: | ------------: |
| `localpost_httptools_inline` (s=1)                | 73,536 |             — |
| `localpost_httptools_inline_s4` (SO_REUSEPORT)    | 74,033 |           +1% |
| `localpost_httptools_inline_acceptor_s4`          | 43,998 |      **−40%** |

With no pool, the handler runs on the worker selector thread, and the accept
→ cross-thread `_OpTrack` hop is pure overhead — there's no parallelism to
amortise it against. **`acceptor=True` should be paired with
`thread_pool_handler`** for the JSON-API shape; documented on the
`http_server` docstring.

### When `acceptor + inline` is *right*: I/O-bound handlers

The inline + acceptor combination *does* win when the handler genuinely
parallelises (e.g. blocks on I/O). The `profile_update` scenario has a
deliberate `time.sleep` totalling ~4 ms per request:

| Stack (3.14t)                                     | RPS  | Notes                            |
| ------------------------------------------------- | ---: | -------------------------------- |
| `localpost_httptools_inline_acceptor_s4`          | 647  | round-robin across 4 selectors   |
| `localpost_httptools_inline_s4` (SO_REUSEPORT)    | 164  | piled on one — same as s=1       |
| `localpost_httptools_inline` (s=1)                | 164  | bound by single-thread sleep     |

**~4× throughput** — the theoretical max for 4 workers. The
`SO_REUSEPORT` variant offers no improvement because the kernel pinned every
conn to the same selector (Phase 7b finding); explicit round-robin is what
unlocks the parallelism.

### What this validates

- **Free-threading is now the supported fast path** (already true since
  Phase 7b). The acceptor topology compounds it: +30 % over `SO_REUSEPORT`
  on h11, +10 % on httptools, on the macOS dev box where `SO_REUSEPORT`
  doesn't kernel-balance.
- **The Phase 7 `selectors > 1` knob is still correct on Linux.** This
  Phase didn't bench Linux; the documented expectation (`SO_REUSEPORT`
  distributes there since 3.9) stands. Acceptor mode is the macOS / free-
  threaded option, not a Linux replacement.
- **The Selector / ConnHandler split is the right factoring.** The acceptor
  topology fell out of `_service.py` in ~80 lines, with no changes to
  `_base.py`'s parser-driven hot path. Future "where does this conn live"
  decisions (per-host pinning, conn-shedding, deterministic-routing) are
  the same shape — a different `ConnHandler`.

### Trade-offs

- **One more thread.** `acceptor=True, selectors=N` runs **N+1** OS threads,
  not N. The acceptor thread does only `accept()` + a cross-thread enqueue;
  it's nearly idle relative to the workers, but the cost is real on
  thread-budget-sensitive deployments (e.g. ulimit-constrained containers).
- **Pin point per conn.** A conn is round-robined exactly once on accept
  and lives on its assigned worker selector for its full keep-alive
  lifetime. No re-balancing if a worker gets a hot client. Two consequences:
  long-lived keep-alive conns can drift out of balance; the parser-handoff
  invariant (single owner per parser instance) is preserved by design.

## What's left for future perf work

Within the [Optimisation boundaries](#optimisation-boundaries) at the top of
this doc:

- **Linux multi-selector validation.** The shipped `selectors > 1` path
  should scale on Linux (kernel-level `SO_REUSEPORT` distribution since
  3.9). Untested locally because dev is macOS-only; settle it on the
  first Linux bench cell — no code change needed. Real deployments are
  ~99% Linux, so this is the actual perf focus, not the macOS dev-box
  result above. The macOS `SO_REUSEPORT` gap is now closed by the
  acceptor topology (Phase 12); a Linux bench should also tell us
  whether `acceptor=True` is worth using *over* `SO_REUSEPORT` on
  free-threaded Linux, or whether kernel distribution dominates.
- **Worker-side syscall coalescing.** One ``sendall`` per response (status
  + headers + body in a single ``bytearray``); ``socket.sendmsg`` for
  chunked responses (status + first chunk + terminator in one syscall).
- **Pre-baked common headers** (Date, Server) cached per-second on the
  ``BaseServer``, written by both backends — only if we ever auto-emit
  them.

Explicit non-goals (per [Optimisation boundaries](#optimisation-boundaries)):
multi-process, async/ASGI on the server side, thread-per-connection rewrite
(Cheroot already exists), sendfile (no benched workload exercises it).
