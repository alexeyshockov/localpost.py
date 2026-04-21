# localpost

A small async Python framework for long-running processes: service hosting,
in-process task scheduler, message broker consumers, and a lightweight HTTP /
OpenAPI stack. Built on [AnyIO](https://anyio.readthedocs.io/) — runs on
asyncio **and** Trio.

Python 3.12+ required.

## Features

- **Hosting** — structured service lifecycle, signal handling, middleware.
- **Scheduler** — declarative triggers (`every`, `after`, `cron`) composed with
  operators like `every("1m") // delay((0, 10))`.
- **Consumers** — in-memory channels, AnyIO streams, `queue.Queue`, Google
  Cloud Pub/Sub (Kafka / SQS / NATS planned).
- **HTTP** — small h11-based sync server; wrap any WSGI app.
- **OpenAPI** — OpenAPI 3.0 inferred from your function signatures, with
  Swagger UI / ReDoc / Scalar docs built in.
- **DI** — `.NET`-style scoped IoC container; optional Flask integration.

## Quick start

```python
import random
import sys
from localpost.hosting import run_app
from localpost.scheduler import after, every, scheduled_task


@scheduled_task(every("3s"))
async def task1():
    return random.randint(1, 22)


@scheduled_task(after(task1))
async def task2(x: int):
    print(f"task1 emitted: {x}")


if __name__ == "__main__":
    sys.exit(run_app(task1, task2))
```

## Docs

- [GitHub README](https://github.com/alexeyshockov/localpost.py) — full overview.
- [Module docs](https://github.com/alexeyshockov/localpost.py/tree/main/localpost) — per-module READMEs.
- [Changelog](https://github.com/alexeyshockov/localpost.py/blob/main/CHANGELOG.md).

MIT licensed.
