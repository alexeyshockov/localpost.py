# localpost.hosting

Service lifecycle management and orchestration. A `service` is any async (or
sync) function wrapped with a lifecycle — it goes through `Starting →
Running → ShuttingDown → Stopped`, reacts to signals, and can spawn child
services in the same task group.

## Quick start

```python
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
    run_app(a_sync_service())
```

`run_app()` wires `shutdown_on_signal()` for you (SIGINT / SIGTERM), runs the
service with AnyIO (picking asyncio or Trio via `choose_anyio_backend`), and
raises `SystemExit` with the resulting status code.

See [`examples/host/finite_service.py`](https://github.com/alexeyshockov/localpost.py/blob/main/examples/host/finite_service.py),
[`examples/host/channel.py`](https://github.com/alexeyshockov/localpost.py/blob/main/examples/host/channel.py).

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

`uvicorn.py` wraps `uvicorn.Server` (with reload and multi-worker disabled);
`hypercorn.py` wraps `hypercorn.asyncio.serve(app, config)` with a shutdown
trigger; `grpc.py` wraps `grpc.aio.Server` with a configurable grace period;
`_asgi.py` holds shared ASGI lifespan helpers. Each adapter is decorated
with `@hosting.service`, so it plugs into `run_app()` the same way as any
other service.

## Host as RSGI for Granian

`localpost.hosting.rsgi.HostRSGIApp` runs the full hosting lifecycle (multiple
services + an HTTP handler) inside each Granian worker. Granian is a process
supervisor that spawns workers and loads our app via its RSGI interface, so
the topology flips: the host *itself* implements RSGI.

```python
from localpost.hosting.rsgi import HostRSGIApp
from localpost.openapi import HttpAsyncApp
from localpost.scheduler import every, scheduled_task


app = HttpAsyncApp()


@app.get("/")
async def root() -> str:
    return "ok"


@scheduled_task(every(seconds=5))
async def heartbeat() -> None: ...


rsgi_app = HostRSGIApp(
    services=[heartbeat.service()],
    rsgi_handler=app,
)

# granian --interface rsgi --workers 4 myapp:rsgi_app
```

`shutdown_on_signal` is **not** applied — Granian owns signal handling;
`__rsgi_del__` is how shutdown reaches us. Every service in `services=`
runs in *each* worker, so cron-style "run once" jobs need either
`--workers 1` or external coordination (DB lock, leader election).

For the bridge layer (RSGI translation, no hosting integration), see
[`localpost.http.rsgi`](http.md#localposthttprsgi). The asymmetry between
uvicorn-as-a-hosted-service and Granian-as-a-supervisor (plus per-worker
lifecycle details) is covered in
[deployment topologies](../design/deployment-topologies.md).

## Implementation notes

- A service may spawn child services via `lt.start(child_svc)` — they run in
  `lt.tg`, so when the parent's service function returns, the child task group
  is cancelled. If you want the children to complete, `await` them explicitly
  before returning.
- `lt.defer(cm)` / `await lt.adefer(acm)` tie a resource's lifetime to the
  service — it's released when the service stops.

## See also

- Examples: [`examples/host/`](https://github.com/alexeyshockov/localpost.py/tree/main/examples/host/)
- Middleware source: [`middleware.py`](https://github.com/alexeyshockov/localpost.py/blob/main/localpost/hosting/middleware.py)
- Core: [`_host.py`](https://github.com/alexeyshockov/localpost.py/blob/main/localpost/hosting/_host.py)
