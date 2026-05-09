# localpost

A small async Python framework for long-running processes: service hosting,
in-process task scheduler, and a lightweight HTTP server. Built on
[AnyIO](https://anyio.readthedocs.io/) — runs on asyncio **and** Trio.

Python 3.12+ required.

## Features

- **Hosting** — structured service lifecycle, signal handling, middleware.
- **Scheduler** — declarative triggers (`every`, `after`, `cron`) composed with
  operators like `every("1m") // delay((0, 10))`.
- **HTTP** — small h11-based sync server; wrap any WSGI app.
- **DI** — `.NET`-style scoped IoC container; optional Flask integration.
- **Free-threaded ready** — pure Python, no C extensions; runs on free-threaded
  CPython 3.14t (no-GIL) with the default `[http]` (h11) backend. Verified by
  the bench: ~3x RPS at `selectors=1`. The optional `[http-fast]` (httptools)
  backend will be no-GIL-clean once httptools 0.8 lands; the currently-released
  0.7.x re-enables the GIL on import.

## Quick start

```python
import random
from localpost.hosting import run_app
from localpost.scheduler import after, every, scheduled_task


@scheduled_task(every("3s"))
async def task1():
    return random.randint(1, 22)


@scheduled_task(after(task1))
async def task2(x: int):
    print(f"task1 emitted: {x}")


if __name__ == "__main__":
    run_app(task1, task2)
```

## Docs

- [GitHub README](https://github.com/alexeyshockov/localpost.py) — full overview.
- [Module docs](https://github.com/alexeyshockov/localpost.py/tree/main/localpost) — per-module READMEs.
- [Changelog](https://github.com/alexeyshockov/localpost.py/blob/main/CHANGELOG.md).

MIT licensed.
