# 0005 — No idle-timeout self-exit in `WorkerExecutor` / `AsyncWorkerExecutor`

- **Status:** Accepted
- **Date:** 2026-05-10

## Context

`localpost.threadtools.WorkerExecutor` and `AsyncWorkerExecutor` were
originally channel-backed worker pools with lazy spawn (driven by
`put_nowait` / `WouldBlock`) up to `max_concurrency`, and **idle-timeout
self-exit**: a worker that saw no work for `idle_timeout` seconds would
close its receiver clone and exit, shrinking the live worker count.

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

With the lifetime guarantee in place, the storage layer was further
simplified from a `Channel` (capacity / clones / broadcast-on-close
machinery, all unused once `max_concurrency` and `backlog` were also
dropped — see follow-on entry below) to a plain
`collections.deque` + `threading.Condition`. The worker loop becomes:

```python
def _run_worker(self) -> None:
    while True:
        with self._cond:
            self._idle += 1
            while not self._queue and not self._closed:
                self._cond.wait()
            self._idle -= 1
            if not self._queue:
                return  # closed and drained
            task = self._queue.popleft()
        task.run()
```

`submit` enqueues under the same condition; if `self._idle == 0` it
spawns a new worker before notifying. There is no cap and no backlog —
concurrency control is the caller's concern.

Alternatives considered:

- **Keep idle timeout, keep the lock dance.** Correct, in good company
  with Java / Tokio. Rejected because the savings don't justify the
  ongoing cost — the duplicated race-handling sat across two classes
  and was the easiest part of the file to break in a refactor without
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
  accept `idle_timeout=`, `max_concurrency=`, or `backlog=`. This is a
  **breaking API change**; given v-next is pre-1.0 and these
  parameters had no non-default use in this repo, no deprecation path
  is provided. Callers that need a cap should apply a
  `threading.Semaphore` / `anyio.CapacityLimiter` upstream of
  `submit`.
- `AsyncExecutor` (per-task spawn gated by `CapacityLimiter`) is
  removed — it had no in-tree callers, and "control concurrency at the
  call site" makes per-task `Future.cancel()` propagation unnecessary.
- `localpost.threadtools.DEFAULT_IDLE_TIMEOUT` is removed from the
  module's public surface.
- The executors no longer depend on `Channel`. `Channel` itself stays
  in the public API for users who want a thread-safe queue with
  capacity / timeouts / broadcast-on-close.
- `WorkerExecutor.workers` (the `Thread` list) and
  `AsyncWorkerExecutor.worker_count` (an int) replace the prior
  `open_receivers` introspection. These were primarily used by tests;
  downstream code that introspected them needs to switch.
- The producer-vs-worker-timeout race is gone with the idle timeout.
  The submit-side lock (now the condition's lock) guards enqueue +
  idle-count check + spawn under one critical section.
- Once at the high-water mark, the pool keeps that many threads alive
  until `__exit__`. For a long-running hosted service this is the
  intended behavior — the pool sizes itself to peak concurrency and
  stays there.
- Memory expectation in production: ~100 KB RSS per parked worker.
  At a peak of, say, 64 concurrent submissions that's ~6 MB held —
  noise next to the Python interpreter and cheaper than re-paying
  `pthread_create` on every burst.
