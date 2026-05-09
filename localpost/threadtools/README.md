# `localpost.threadtools`

Thread-friendly building blocks for moving work off the event loop:

- [`Channel`](#channel) — a typed, thread-safe queue with `timeout` on `put` / `get` and broadcast-on-close.
- [`Executor`](#executors) — three implementations, one `submit` contract.
- [`TaskGroup`](#taskgroup) — Trio-style structured concurrency over an `Executor`.
- [`run_async`](#run_async) — sync→async bridge: dispatch a coroutine onto the current service's loop from a worker thread.

`localpost.threadtools` is built on plain locks; the AnyIO loop is needed only for the `Async…Executor` variants.

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

Three implementations share the same `submit(fn, *args, **kwargs) -> Future` contract. Lifecycle and cancellation differ:

| Executor                | CM           | Cancellation                        | Loop required |
|-------------------------|--------------|-------------------------------------|---------------|
| `WorkerExecutor`        | `with`       | None — workers finish naturally     | No            |
| `AsyncWorkerExecutor`   | `async with` | Per-worker (`stop()` / scope cancel)| Yes           |
| `AsyncExecutor`         | `async with` | Per-task (`Future.cancel()`)        | Yes           |

All three propagate `contextvars.Context` to the task, matching `asyncio.to_thread` / Trio / AnyIO spawn semantics.

### `WorkerExecutor` — sync, channel-backed

Plain `threading.Thread` workers. Lazy spawn, idle-timeout self-exit. No event loop.

```python
from localpost.threadtools import WorkerExecutor

with WorkerExecutor(max_concurrency=8) as ex:
    fut = ex.submit(work, x)
    result = fut.result()
```

`submit` tries `put_nowait` first; on `WouldBlock` it spawns a new worker (up to `max_concurrency`) and falls through to a blocking `put` (which is what backpressures the caller when at the cap).

### `AsyncWorkerExecutor` — channel + AnyIO threadlocals

Same channel / lazy-spawn shape as `WorkerExecutor`, but workers run inside `anyio.to_thread.run_sync(..., abandon_on_cancel=False)` so user code can call `anyio.from_thread.check_cancelled`.

The worker's cancel scope spans its whole lifetime (one worker handles many tasks), so cancellation granularity is **per-worker**, not per-task. Use `stop()` for fast cooperative shutdown.

```python
async with anyio.from_thread.BlockingPortal() as portal:
    async with AsyncWorkerExecutor(portal=portal) as ex:
        fut = await anyio.to_thread.run_sync(ex.submit, work, x)
        # …
        await anyio.to_thread.run_sync(ex.stop)   # cancel everything
```

`submit` and `stop` must be invoked from a non-loop thread (use `await anyio.to_thread.run_sync(…)` from inside async code).

### `AsyncExecutor` — fresh AnyIO task per submit

Every `submit` schedules a fresh AnyIO task on the executor's internal task group; concurrency is gated by an `anyio.CapacityLimiter` (`math.inf` = no cap). Cancellation is **per-task**: `Future.cancel()` propagates to the underlying task's `check_cancelled`.

```python
async with anyio.from_thread.BlockingPortal() as portal:
    async with AsyncExecutor(portal=portal, max_concurrency=4) as ex:
        fut = await anyio.to_thread.run_sync(ex.submit, work, x)
        # cancel just this task:
        fut.cancel()
```

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

`TaskGroup` is **collect-and-raise only** — running tasks are not interrupted on the first failure. If you want cooperative cancel, give it an `AsyncWorkerExecutor` / `AsyncExecutor` so tasks can poll `check_cancelled`.

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

Resolves the portal via `localpost.hosting.current_service`, so the calling thread must inherit the hosting context (true for any thread spawned through AnyIO or the executors above). Must be called from a non-loop thread; on the loop thread the underlying `BlockingPortal.call` raises `RuntimeError`.

## Composing with `localpost.hosting`

`localpost.hosting._serve_root` already runs a single `BlockingPortal` for the whole app. It's exposed on the service lifetime view so you can layer an executor on top of it without opening a second portal:

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
