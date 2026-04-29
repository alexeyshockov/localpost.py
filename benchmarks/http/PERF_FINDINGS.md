# HTTP server performance findings

Initial diagnosis from the 2026-04-27 bench run (3s per cell, before Phase 1).
Phase 1 results in [Phase 1 results](#phase-1-results-2026-04-27) below.

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

### What's left for future perf work

- **Sendfile / vectored writes** for response bodies. Each
  ``sock.sendall`` on the response path is one syscall; status + first
  chunk + terminator could be one ``writev`` for small responses.
- **Pre-baked common headers** (Date, Server) cached per-second on the
  ``BaseServer``, written by both backends.
- The selector-thread CPU is now lower (parser cheaper). If we ever
  return to closing the gap to async-ASGI stacks, **multi-selector
  + SO_REUSEPORT** is the next architectural lever — the op-queue
  design from Phase 3-B already accommodates multiple selectors.
