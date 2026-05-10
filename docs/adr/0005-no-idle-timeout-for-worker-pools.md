# 0005 — No idle-timeout self-exit in `WorkerExecutor` / `AsyncWorkerExecutor`

- **Status:** Accepted
- **Date:** 2026-05-10

## Context

`localpost.threadtools.WorkerExecutor` and `AsyncWorkerExecutor` are
channel-backed worker pools with lazy spawn (driven by
`put_nowait` / `WouldBlock`) up to `max_concurrency`. The original
design also had **idle-timeout self-exit**: a worker that saw no work
for `idle_timeout` seconds would close its receiver clone and exit,
shrinking the live worker count.

That feature interacted badly with the rest of the design:

- Each worker held its own cloned `rx`. The executor kept a long-lived
  `_rx_template` open so that, after the last worker exited, the next
  `put_nowait` wouldn't raise `ClosedResourceError`. The template was a
  load-bearing placeholder, not a real consumer — present in the
  channel's `open_receive_channels` count but unable to receive.
- The combination created a race between worker timeout and a producer
  whose `put_nowait` failed (rendezvous full, no waiting receiver). With
  `_rx_template` keeping the channel "open", the channel could never
  signal "no consumers" to the blocking `put` Phase 2 — so an item
  buffered by a producer right as the last worker exited would sit in
  the buffer forever, and `put` would block forever waiting for
  `items_consumed` to advance.
- The fix was a `get_nowait` recheck under the executor's lock in the
  worker's `TimeoutError` handler: if a producer slipped a task in
  between the worker's wait timeout and its decision to die, the worker
  would still take it. This worked, but the lifecycle bookkeeping
  (`_open_receivers` list, per-worker `rx.clone()`, defensive
  `rx in self._open_receivers` / `rx.close()` on every exit path) was
  duplicated across both executors.

Forces in play when reconsidering:

- **The race is fundamental to lazy-spawn-with-idle-timeout.** A survey
  of channel libraries (Rust `mpsc` / `crossbeam` / `flume` / `tokio`,
  Go built-in channels, Java `BlockingQueue`, AnyIO `MemoryObjectStream`)
  confirms that no mainstream channel library absorbs the race into the
  channel API — channels are dumb queues; pools manage lifecycle. The
  one mainstream system with the same shape, Java
  `ThreadPoolExecutor`, resolves the race in the pool too (packed
  `(runState, workerCount)` atomic + `addWorker(null, false)` recheck
  from `execute()`). Tokio's blocking pool resolves it the same way
  with a coarse `Mutex`. So the lock-dance is in good company — *if*
  we keep the feature.
- **The savings idle scale-down promises are largely illusory for
  in-process Python worker pools.** A worker parked in
  `condvar.wait` consumes <100 KB RSS (kernel commits only touched
  pages of the 8 MB virtual stack), is not scheduled, and costs the
  next burst a fresh `pthread_create` (hundreds of µs to a few ms) to
  rebuild. The mechanism complexity (the race, the placeholder
  receiver, the duplicated handlers) buys back a small amount of RSS
  that gets immediately re-spent on the next workload spike.
- **Reference implementations vote with their defaults.** Python's
  stdlib `concurrent.futures.ThreadPoolExecutor` has no idle timeout
  at all — workers live until `shutdown()`. Java's
  `ThreadPoolExecutor` defaults `allowCoreThreadTimeOut` to `false`.
  Idle scale-down is a feature people *can* opt into in those
  ecosystems, not the baseline behavior.
- **Where idle scale-down genuinely matters** (per-thread DB
  connections, GPU contexts, large per-thread caches), none of those
  apply to `WorkerExecutor`'s contract — workers just run callables
  with a propagated `contextvars.Context`. Per-task expensive
  resources belong inside the task, not pinned to a long-lived worker.

## Decision

Workers in `WorkerExecutor` and `AsyncWorkerExecutor` live for the
**executor's lifetime**. There is no idle-timeout self-exit, and no
`idle_timeout=` parameter on either constructor. The
`DEFAULT_IDLE_TIMEOUT` module constant is removed.

Two structural simplifications follow naturally and are also part of
this decision:

1. **One shared receiver** instead of per-worker `rx.clone()`. Workers
   all pull from the executor's single `self._rx`; the channel's state
   lock already serializes concurrent `get` calls correctly. This
   matches Tokio's blocking pool design.
2. **No `_rx_template` placeholder.** With workers staying alive until
   close, there is no inter-worker generation gap to bridge. The
   executor holds `tx` and `rx` directly for its whole lifetime.

The worker loop reduces to:

```python
def _run_worker(self) -> None:
    for task in self._rx:
        task.run()
```

Submit keeps its existing two-phase shape (`put_nowait` → spawn →
blocking `put`), but the cap check uses `len(self._workers)` /
`self._worker_count` and the spawn path no longer manages cloned
receivers.

Alternatives considered:

- **Keep idle timeout, keep the lock dance.** Correct, in good company
  with Java / Tokio. Rejected because the savings don't justify the
  ongoing cost — the duplicated race-handling sits across two classes
  and is the easiest part of the file to break in a refactor without
  noticing.
- **Push the race resolution into the channel API** (e.g. a
  `BrokenChannel` signal when the last receiver dies, paired with a
  receiver factory that the producer can use to spawn a new consumer
  on retry). Rejected after the cross-language survey: no mainstream
  channel library does this, because channels and pools are different
  layers and conflating them leaks pool concerns into the channel.
  We'd be inventing a new abstraction with no peers.

## Consequences

- `WorkerExecutor(...)` and `AsyncWorkerExecutor(...)` no longer
  accept `idle_timeout=`. This is a **breaking API change**; given
  v-next is pre-1.0 and the parameter never had a non-default use in
  this repo, no deprecation path is provided.
- `localpost.threadtools.DEFAULT_IDLE_TIMEOUT` is removed from the
  module's public surface.
- `WorkerExecutor.open_receivers` and `AsyncWorkerExecutor.open_receivers`
  are replaced by `WorkerExecutor.workers` (the `Thread` list) and
  `AsyncWorkerExecutor.worker_count` (an int). These were primarily
  used by tests; downstream code that introspected them needs to
  switch.
- The `_run_worker` method on both executors collapses to a 2-line
  `for task in self._rx: task.run()` loop. The `_open_receivers` list,
  the per-worker `rx.clone()` / `rx.close()` bookkeeping, and the
  `_rx_template` placeholder are gone.
- The producer-vs-worker-timeout race no longer exists, so the
  `get_nowait` recheck under `self._lock` in the worker's
  `TimeoutError` handler is gone too. The submit-side lock still
  guards the cap check + spawn (cap accounting is the executor's
  concern; the channel doesn't track that).
- Once at the high-water mark, the pool keeps that many threads alive
  until `__exit__`. For a long-running hosted service this is the
  intended behavior — the pool sizes itself to peak concurrency and
  stays there. Callers that need bursty work without a sticky pool
  should use `AsyncExecutor` (per-task spawn gated by a
  `CapacityLimiter`), which already exists for exactly this case.
- Memory expectation in production: ~100 KB RSS per parked worker.
  At a `max_concurrency` of, say, 64, that's ~6 MB held — noise next
  to the Python interpreter and cheaper than re-paying
  `pthread_create` on every burst.
