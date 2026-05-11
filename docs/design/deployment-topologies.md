# Deployment topologies — uvicorn vs Granian

`localpost` supports three async deployment targets: **uvicorn**,
**hypercorn**, and **Granian**. The first two are *in-process* — they
run as a hosted service inside `hosting.run_app(...)`, alongside any
other services you've registered. Granian is *out-of-process* — it's
a process supervisor that spawns workers and loads our app via its
RSGI interface.

The two cases look superficially similar (both serve HTTP, both run
async), but the deployment topology is inverted. This note explains
why and how the framework's API surface reflects that.

## In-process: uvicorn / hypercorn

```
┌─────────────────────────────────────────────┐
│ Process owned by localpost.hosting          │
│  ├─ scheduler service                       │
│  ├─ uvicorn_server(config) service          │
│  │   └─ uvicorn.Server.serve()              │
│  │       └─ ASGI app                        │
│  └─ … other hosted services                 │
└─────────────────────────────────────────────┘
```

`uvicorn.Server` runs *inside* our process, configured with `workers=1`.
Hosting drives its lifecycle: on `set_started` we register endpoints,
on `shutting_down` we set `should_exit = True` and wait for the
serve loop to return. Memory is shared between the HTTP app and every
other hosted service.

API:

```python
hosting.run_app([
    scheduler.service(),
    app.service(uvicorn.Config(...)),     # server="uvicorn" (default)
    app.service(hypercorn.Config(...), server="hypercorn"),
])
```

Same pattern as gRPC / any other adapter in `localpost.hosting.services/`.

## Out-of-process: Granian

```
┌────────────────────────────────────────────────────────────────────┐
│ Granian (process supervisor, spawned by `granian` CLI)             │
│  ├─ Worker process 1                                               │
│  │   └─ HostRSGIApp (single localpost process)                     │
│  │       ├─ scheduler service       ─┐                             │
│  │       ├─ other hosted services   ─┼─ all running in-process     │
│  │       └─ RSGI request handler    ─┘  in this worker             │
│  ├─ Worker process 2 (same shape)                                  │
│  └─ Worker process N                                               │
└────────────────────────────────────────────────────────────────────┘
```

Granian doesn't have an in-process mode you can plug into
`hosting.run_app(...)`. It always spawns workers (one per CPU by
default). Each worker is its own Python process, loads the user's
module, picks up the RSGI app object, and serves requests.

To deploy a hosted app under Granian, the **host itself implements
RSGI**. Inside each Granian worker process, `HostRSGIApp` runs the
full hosting stack (scheduler + other services + the HTTP request
handler) — sharing memory the way a regular `hosting.run_app`
deployment does. The HTTP request handler is dispatched per-request
via Granian's RSGI hook.

API:

```python
from localpost.hosting.rsgi import HostRSGIApp

rsgi_app = HostRSGIApp(
    services=[scheduler.service(), other_service.service()],
    rsgi_handler=app,                  # HttpAsyncApp or AsyncRequestHandler
)
# granian --interface rsgi --workers 4 myapp:rsgi_app
```

## Why the asymmetry

It's a property of the host server, not a framework choice:

- **uvicorn / hypercorn** are pure-Python ASGI servers. They expose
  programmatic APIs (`Server.serve()`, `serve(app, config)`) that run
  on whatever event loop you give them. Wrapping one as a hosted
  service is natural.
- **Granian** is a Rust-native RSGI server with multi-process workers
  baked in. The Python side is a thin shim Granian calls *into* — you
  can't call Granian *from* an existing event loop and expect it to
  share that loop with the rest of your app.

Trying to run Granian as a `hosting.run_app(...)` service would force
either single-worker (defeats Granian's main feature) or a
process-detach (defeats the in-process service-sharing model
`hosting.run_app` is built around). Inverting the topology — Granian
spawns the workers; each worker runs `hosting.serve(...)` — keeps both
sides honest.

## Per-worker side effects (Granian only)

A consequence of the supervisor topology: every Granian worker runs
the *full* `services=` list. With `--workers 4`, four schedulers tick.
Cron-style "run once" jobs need either:

- `--workers 1` — fine for low-RPS services where one Python worker
  saturates upstream capacity.
- External coordination — DB lock, leader election, or a separate
  cron service running outside the HTTP fleet.

Process-shared state across workers doesn't exist (no
`multiprocessing.Manager` integration in scope). That's normal
multi-process territory; the framework doesn't try to hide it.

## Lifecycle hook semantics

Granian's `__rsgi_init__(loop)` and `__rsgi_del__(loop)` hooks are
**synchronous** — Granian calls them without `await`. This means
`HostRSGIApp` can't actually wait for `hosting.serve()` to reach
`started` inside `__rsgi_init__`. The implementation is:

- `__rsgi_init__`: spawn a long-lived task on the supplied loop; the
  task enters `serve()` and waits for an internal "shutdown" event
  before exiting. Set a "ready" `asyncio.Event` once services are up.
- `__rsgi__`: gate the first request on the ready event — Granian may
  start dispatching before the lifecycle is fully up.
- `__rsgi_del__`: signal the lifecycle task to exit its `serve()`
  block; hosting drives shutdown and waits for `stopped`.

The single-task pattern is required because anyio's cancel scopes
(used inside `serve()`) must be entered and exited by the same task —
splitting `__aenter__` / `__aexit__` across two would raise
`"different task than it was entered in"`.

## Picking a target

| You want…                              | Use                                          |
|----------------------------------------|----------------------------------------------|
| Simplest deployment, scaling via external supervisor | uvicorn / hypercorn via `app.service(config)` |
| Maximum HTTP throughput on Python      | Granian via `app.as_rsgi()` or `HostRSGIApp` |
| Multiple hosted services + Granian     | `HostRSGIApp(services=..., rsgi_handler=app)` |
| Multiple hosted services + ASGI server | `app.service(uvicorn.Config(...))` alongside other services in `run_app` |
| HTTP only, ASGI server                 | `app.asgi()` directly (`granian --interface asgi`) |
| HTTP only, RSGI server                 | `app.as_rsgi()` directly (`granian --interface rsgi`) |
