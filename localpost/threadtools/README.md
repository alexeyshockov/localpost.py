# `localpost.threadtools`

Thread-friendly building blocks for moving work off the event loop:

- [`Channel`](#channel) — a typed, thread-safe queue with `timeout` on `put` / `get` and broadcast-on-close.
- [`Executor`](#executors) — two implementations, one `submit` contract.
- [`Portal`](#portal) — thread-aware view over `anyio.from_thread.BlockingPortal`.
- [`TaskGroup`](#taskgroup) — Trio-style structured concurrency over an `Executor`.
- [`run_async`](#run_async) — sync→async bridge: dispatch a coroutine onto the current service's loop from a worker thread.

`localpost.threadtools` is built on plain locks; the AnyIO loop is needed only for the `AsyncWorkerExecutor`.

## Channel

```python
from localpost.threadtools import Channel

tx, rx = Channel.create(capacity=8)
tx.put(item, timeout=1.0)        # raises TimeoutError on expiry
got = rx.get(timeout=1.0)
got = rx.get_nowait()            # raises WouldBlock / EndOfStream
```

`capacity=None` is unbounded; `0` is rendezvous (put waits until a receiver consumes); `N>0` is bounded. Both ends can be cloned (`tx.clone()`, `rx.clone()`); closing either side broadcasts to every waiter so cloned receivers all observe `EndOfStream` / `ClosedResourceError`.

## Executors

Two implementations share the same `submit(fn, *args, **kwargs) -> Future` contract. Both are spawn-on-demand pools with no cap and no backlog — concurrency is the caller's concern (a Cloud Run-like upstream gate, a consumer-level `Semaphore`, etc.). Both expose `worker_count: int` and a `stop()` method that's safe to call from any thread. Lifecycle and the effect of `stop()` differ:

| Executor                | CM           | `stop()`                                                              | Loop required |
|-------------------------|--------------|-----------------------------------------------------------------------|---------------|
| `WorkerExecutor`        | `with`       | Soft close — idle workers exit, busy workers finish their current task| No            |
| `AsyncWorkerExecutor`   | `async with` | Soft close + cancel scope (per-worker `check_cancelled`)              | Yes           |

Both propagate `contextvars.Context` to the task, matching `asyncio.to_thread` / Trio / AnyIO spawn semantics.

### `WorkerExecutor` — sync, deque + Condition

Plain `threading.Thread` workers. Lazy spawn: `submit` enqueues onto a shared `deque` under a `threading.Condition`; if no worker is idle, a new one is spawned. Workers live until the executor closes (no idle-timeout self-exit — see [ADR-0005](../../docs/adr/0005-no-idle-timeout-for-worker-pools.md)). No event loop.

```python
from localpost.threadtools import WorkerExecutor

with WorkerExecutor() as ex:
    fut = ex.submit(work, x)
    result = fut.result()
```

`stop()` is safe to call from any thread (e.g. a signal handler): it marks the pool closed and wakes idle workers so they exit; busy workers finish their current task and then exit on the next loop iteration. Subsequent `submit` calls raise `RuntimeError`.

### `AsyncWorkerExecutor` — deque + AnyIO threadlocals

Same shape as `WorkerExecutor`, but workers run inside `anyio.to_thread.run_sync(..., abandon_on_cancel=False)` so user code can call `anyio.from_thread.check_cancelled`.

The worker's cancel scope spans its whole lifetime (one worker handles many tasks), so cancellation granularity is **per-worker**, not per-task. Use `stop()` for fast cooperative shutdown.

```python
from localpost import Portal

async with anyio.from_thread.BlockingPortal() as raw_portal:
    portal = Portal(raw_portal)
    async with AsyncWorkerExecutor(portal=portal) as ex:
        fut = await anyio.to_thread.run_sync(ex.submit, work, x)
        # …
        ex.stop()                                  # safe from any thread
```

`submit` and `stop` are safe to call from any thread — the wrapping `Portal` does the on-loop / off-loop dispatch internally.

## TaskGroup

A thin sync bookkeeping layer over an `Executor`. Tracks `Future`s; on `__exit__` waits for every submitted task and re-raises failures as a `BaseExceptionGroup` (Trio `strict_exception_groups=True` semantics — body and task exceptions are merged into one group).

```python
from localpost.threadtools import TaskGroup, WorkerExecutor

with WorkerExecutor() as ex:
    with TaskGroup(ex) as tg:
        tg.start_soon(do_work, arg)         # fire-and-forget
        fut = tg.create_task(other_work)    # observe via Future
    # On exit: drain in-flight tasks; raise BaseExceptionGroup if any failed.
```

`TaskGroup` is **collect-and-raise only** — running tasks are not interrupted on the first failure. If you want cooperative cancel, give it an `AsyncWorkerExecutor` so tasks can poll `check_cancelled`.

## `run_async`

The reverse of `anyio.to_thread.run_sync` — call from a worker thread to dispatch an async function back onto the loop and wait for its result.

```python
from localpost.threadtools import run_async


async def fetch_user(user_id: int) -> User:
    ...


def worker(user_id: int) -> str:
    user = run_async(fetch_user, user_id)  # blocks the worker thread
    return user.name
```

Resolves the portal via `localpost.hosting.current_service`, so the calling thread must inherit the hosting context (true for any thread spawned through AnyIO or the executors above). Must be called from a non-loop thread; raises `RuntimeError` otherwise (would deadlock the loop).

## Portal

`Portal` wraps `anyio.from_thread.BlockingPortal` with loop-thread awareness. Construct it on the loop thread (it snapshots `threading.get_ident()` at creation), then pass it to `AsyncWorkerExecutor` or any code that needs to schedule onto the loop without caring about the calling thread.

```python
from localpost import Portal

portal.same_thread             # is the current thread the loop thread?
portal.run_sync(fn, *args)     # call sync fn on the loop, return its result
portal.run_async(coro, *args)  # await coro on the loop, off-loop only
portal.raw                     # underlying BlockingPortal (escape hatch)
```

`run_sync` does the right thing in either direction: direct call on-loop, `BlockingPortal.call` off-loop. `run_async` raises `RuntimeError` on the loop thread instead of deadlocking.

## Composing with `localpost.hosting`

`localpost.hosting._serve_root` already runs a single `Portal` for the whole app. It's exposed on the service lifetime view so you can layer an executor on top of it without opening a second portal:

```python
from localpost import hosting
from localpost.threadtools import AsyncWorkerExecutor

@hosting.service
async def my_service():
    portal = hosting.current_service().portal
    async with AsyncWorkerExecutor(portal=portal) as ex:
        # … use ex.submit from worker threads …
        yield
```

This is how `localpost.http.HttpApp.service` and `localpost.openapi.HttpApp.service` default their internal pool when no `executor=` is passed — handlers automatically get `from_thread.check_cancelled` support.
