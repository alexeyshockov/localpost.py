# Getting started

## Install

```bash
pip install localpost
```

For the scheduler and HTTP examples below you'll want a couple of extras:

```bash
pip install 'localpost[scheduler,http]'
```

See the [extras table on the home page](index.md#install) for the full list.

## A scheduled task

The scheduler triggers callables on **conditions** — `every(...)`,
`after(...)`, `cron(...)` — composed with operators. `run_app` from the hosting
module wires up signal handling and runs everything until SIGINT / SIGTERM.

```python
import random
import sys

from localpost.hosting import run_app
from localpost.scheduler import after, delay, every, scheduled_task, take_first


@scheduled_task(every("3s") // delay((0, 1)))
async def task1():
    return random.randint(1, 22)


@scheduled_task(after(task1) // take_first(3))
async def task2(task1_result: int):
    print(f"task1 emitted: {task1_result}")


if __name__ == "__main__":
    sys.exit(run_app(task1, task2))
```

What's happening:

- `every("3s") // delay((0, 1))` — fire every 3 seconds, jittered by 0–1s.
- `after(task1) // take_first(3)` — fire when `task1` completes, take only its
  first 3 emissions.
- `run_app(...)` — start every service in parallel, exit cleanly when they all
  stop.

## A sync function works too

The scheduler accepts both sync and async callables. Sync ones are offloaded
to a thread pool via `anyio.to_thread`:

```python
from localpost.scheduler import every, scheduled_task


@scheduled_task(every("10s"))
def collect_metrics():  # plain `def`, no `async`
    ...
```

## Where to next

- **[hosting](modules/hosting.md)** — service lifecycles, signal handling,
  middleware, and adapters for Uvicorn / Hypercorn / gRPC.
- **[scheduler](modules/scheduler.md)** — full trigger reference, operators,
  cron support.
- **[http](modules/http.md)** — the lightweight HTTP/1.1 server, WSGI / ASGI
  bridges, router.
- **[di](modules/di.md)** — scoped IoC container.
- **[openapi](modules/openapi.md)** — typed HTTP framework with built-in
  OpenAPI 3.2 generation.

Working examples for every module live in
[`examples/`](https://github.com/alexeyshockov/localpost.py/tree/main/examples)
in the repository.
