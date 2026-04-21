# localpost.hosting

> **Status:** stable — public API is not expected to break in patch/minor releases.

Service lifecycle management and orchestration. A `service` is any async (or
sync) function wrapped with a lifecycle — it goes through `Starting →
Running → ShuttingDown → Stopped`, reacts to signals, and can spawn child
services in the same task group.

## Quick start

```python
import sys
import time
from localpost.hosting import ServiceLifetime, run_app, service


@service
def a_sync_service():
    def svc(lt: ServiceLifetime):
        print("Service started")
        lt.set_started()
        print("Service running")
        time.sleep(5)
        print("Service is done")  # host stops when all services stop
    return svc


if __name__ == "__main__":
    sys.exit(run_app(a_sync_service()))
```

`run_app()` wires `shutdown_on_signal()` for you (SIGINT / SIGTERM), runs the
service with AnyIO (picking asyncio or Trio via `choose_anyio_backend`), and
returns an exit code.

See `examples/host/finite_service.py`, `examples/host/channel.py`.

## Key concepts

- **`ServiceLifetime`** — the handle passed to every service. Exposes
  `started`, `shutting_down`, `stopped` events (as `Event` / `EventView`),
  an anyio `TaskGroup` (`lt.tg`) for spawning child tasks, and `defer` /
  `adefer` to stash context managers / closable resources.
- **`ServiceState`** — the union `Starting | Running | ShuttingDown | Stopped`
  (immutable dataclasses). Accessible via `lt.view.state`.
- **`@service` decorator** — turns a factory (returning a service function
  or async generator) into a `_ResolvedService`, a callable that doubles as
  an async context manager.
- **Middleware** — ordinary function decorators over the service function.
  Examples: `shutdown_on_signal(*signals)`, `start_timeout(seconds)`.
- **`current_service()` / `current_app()`** — read-only views of the enclosing
  lifetimes via contextvars, without threading them through every call.

## Public API

| Symbol                           | Where                | Notes                                      |
| -------------------------------- | -------------------- | ------------------------------------------ |
| `service`                        | decorator            | Wrap a factory into a hosted service       |
| `run_app(*services)`             | entry point          | Signal handling + `anyio.run`              |
| `run(svc, parent=None)`          | low-level            | Run a single service, return exit code     |
| `serve(svc, *, parent=None)`     | low-level            | Async CM yielding `ServiceLifetimeView`    |
| `observe_services(*lifetimes)`   | async CM             | Wait for all to start; shut down on exit   |
| `current_service()`              | contextvar accessor  | Raises if outside a hosting context        |
| `ServiceLifetime`                | dataclass            | Mutable lifetime (internal-ish)            |
| `ServiceLifetimeView`            | dataclass            | Read-only view + `observe()`, `shutdown()` |
| `ServiceState`                   | type alias           | `Starting` \| `Running` \| `ShuttingDown` \| `Stopped` |
| `Starting` / `Running` / `ShuttingDown` / `Stopped` | dataclasses | Individual states            |
| `shutdown_on_signal(*signals)`   | middleware           | SIGINT + SIGTERM by default                |

## Writing a service

Four signatures are supported; `@service` picks the right adapter:

1. **Async function** — `async def svc(lt: ServiceLifetime) -> None`
2. **Sync function** — runs in a worker thread via `to_thread.run_sync`
3. **Async generator** — `@service async def factory(): setup; yield; teardown`
   (wrapped with `@asynccontextmanager`; `lt.set_started()` is called after
   `yield`-in).
4. **Factory returning one of the above**

Always call `lt.set_started()` once your service is ready. Services that
never call it block `observe_services` forever.

## Writing middleware

Middleware is a plain decorator over `ServiceF = Callable[[ServiceLifetime],
Awaitable[None]]`. Reference: `shutdown_on_signal` in `middleware.py`:

```python
def my_middleware(arg) -> Callable[[ServiceF], ServiceF]:
    def decorator(func: ServiceF) -> ServiceF:
        @wraps(func)
        def wrapper(lt: ServiceLifetime) -> Awaitable[None]:
            lt.tg.start_soon(my_background_task, lt.view)
            return func(lt)
        return wrapper
    return decorator
```

## Adapters for external servers (`services/`)

| Adapter       | What it wraps                             |
| ------------- | ----------------------------------------- |
| `uvicorn.py`  | `uvicorn.Server` (`config` input; reload and multi-worker disabled) |
| `hypercorn.py`| `hypercorn.asyncio.serve(app, config)` with a shutdown trigger |
| `grpc.py`     | `grpc.aio.Server` with configurable grace period |
| `_asgi.py`    | Shared ASGI lifespan helpers              |

Each adapter is decorated with `@hosting.service`, so it plugs into
`run_app()` the same way as any other service.

## Implementation notes

- A service may spawn child services via `lt.start(child_svc)` — they run in
  `lt.tg`, so when the parent's service function returns, the child task group
  is cancelled. If you want the children to complete, `await` them explicitly
  before returning.
- `lt.defer(cm)` / `await lt.adefer(acm)` tie a resource's lifetime to the
  service — it's released when the service stops.

## See also

- Examples: [`examples/host/`](../../examples/host/)
- Middleware source: [`middleware.py`](middleware.py)
- Core: [`_host.py`](_host.py)
