# HTTP threading topologies

`http_server` supports three accept-side shapes, all driving the same
`RequestHandler` (single-shot dispatch on headers-complete). The
underlying connection state machine is shared — see
[connection-model.md](connection-model.md) for the TRACKED / BORROWED
diagram and cross-thread sync edges.

| Configuration                   | Threads                                                                          | When to use                                                                                                                                                                              |
| ------------------------------- | -------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Default (`selectors=1`)         | 1 thread accepts, parses, and dispatches                                         | Simple deployments; the thread-pool wrapper handles concurrency on the request side                                                                                                      |
| `selectors=N`                   | N independent selectors, each with its own listening socket via `SO_REUSEPORT`   | Linux kernel-level connection load-balancing; free-threaded builds                                                                                                                       |
| `selectors=N, acceptor=True`    | 1 acceptor thread + N worker selector threads                                    | macOS / free-threaded targets where `SO_REUSEPORT` doesn't distribute evenly. Acceptor accepts each conn and round-robins it to a worker via the cross-thread op queue.                  |

The server loop runs in a worker thread
(`anyio.to_thread.run_sync`); shutdown is driven by
`lt.shutting_down` via `threadtools.check_cancelled()`. The server
hosts a single handler; whether that handler runs synchronously on
the selector thread or hops to a worker is the handler's choice.

## Composition pattern

`http_server`, the `Router`, and `thread_pool_handler` are three
orthogonal concepts that you compose explicitly:

```python
from localpost.hosting import run_app, service
from localpost.http import (
    Routes, ServerConfig, http_server, thread_pool_handler,
)


@service
async def app():
    routes = Routes()

    @routes.get("/hello/{name}")
    def hello(ctx): ...   # plain RequestCtx → Response handler

    config = ServerConfig(host="127.0.0.1", port=8000)
    async with thread_pool_handler(routes.build().as_handler()) as h:
        async with http_server(config, h):
            yield


run_app(app())
```

What this gives you:

- **404 / 405 stay on the selector thread.** When the `Router` is
  the handler (wrapped or not), unmatched paths and method
  mismatches go through `_send_plain` inline — no worker hop.
- **Matched routes run wherever you want.** Wrap the entire router
  with `thread_pool_handler` (above) to run all matched handlers on
  workers; pass the router directly to `http_server` to keep them
  all on the selector; more granular per-route control is the
  user's composition problem (today there is no per-route pool
  API).
- **No concurrency cap on `http_server` or the pool.** The pool
  dispatches every request onto a process-wide `ThreadTaskGroup`;
  workers are spawned on demand and reused across all
  `thread_pool_handler` instances in the process. There is no
  admission gate and no 503-on-overflow — backpressure is the
  deployment's concern (front-LB / OS thread limits).

## Three orthogonal concerns

- **Handler** — `Callable[[HTTPReqCtx], None]`. Immediate by default
  (runs on whichever thread invokes it). The thread-pool variant is
  just a wrapper that borrows the connection, queues it, and runs
  `inner` on a worker.
- **Router** — itself an immediate handler. Runs `_match()` inline
  on the calling thread; 404/405 are sent via `_send_plain` and the
  conn re-tracks via the existing connection loop. Matched routes
  call the registered per-route handler — which the user can choose
  to pool or not.
- **`http_server`** — hosting integration only. Owns the AnyIO
  bridge, drives `server.run()` in a thread, watches
  `lt.shutting_down`. Doesn't know about pools, doesn't know about
  routing.

This split means the simple case (all-immediate handlers) doesn't
pay for a worker pool, and the Router's 404/405 path doesn't either
even when the matched routes run on workers.
