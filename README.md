# localpost

[![PyPI package version](https://img.shields.io/pypi/v/localpost)](https://pypi.org/project/localpost/)
![Python versions](https://img.shields.io/pypi/pyversions/localpost)
[![Code coverage](https://img.shields.io/sonar/coverage/alexeyshockov_localpost.py?server=https%3A%2F%2Fsonarcloud.io)](https://sonarcloud.io/project/overview?id=alexeyshockov_localpost.py)

A small async Python framework for long-running processes: service hosting,
in-process task scheduling, message broker consumers, and a lightweight HTTP /
OpenAPI stack — all built on [AnyIO](https://anyio.readthedocs.io/) (runs on
asyncio **and** Trio).

LocalPost is not a monolith. Each module is usable on its own; pick what you
need.

## Features

- **Service hosting** with a structured lifecycle (`Starting → Running →
  ShuttingDown → Stopped`), signal handling, and composable middleware.
- **Scheduler** with declarative triggers (`every`, `after`, `cron`) and
  operator-based composition (`every("1m") // delay((0, 10))`).
- **Consumers** for in-memory channels, AnyIO streams, `queue.Queue`, and
  Google Cloud Pub/Sub — accepting both sync and async handlers.
- **HTTP server** — sync, h11-based, ~400 LOC; wrap any WSGI app.
- **OpenAPI layer** — type-driven; OpenAPI 3.0 spec is inferred from your
  function signatures, with Swagger UI / ReDoc / Scalar docs.
- **IoC container** — `.NET`-style, scoped, with Flask integration.

## Install

```bash
pip install localpost
```

Optional extras:

| Extra             | Adds                                                        |
| ----------------- | ----------------------------------------------------------- |
| `[cron]`          | `croniter` — cron-expression trigger                        |
| `[scheduler]`     | `humanize`, `pytimeparse2` — string durations               |
| `[http-server]`   | `h11` — the HTTP server                                     |
| `[http-openapi]`  | `msgspec` — OpenAPI serialization                           |
| `[sqs]`           | `botocore` — AWS SQS                                        |
| `[kafka]`         | `confluent-kafka`                                           |
| `[nats]`          | `nats-py`                                                   |
| `[pubsub]`        | `google-cloud-pubsub`                                       |
| `[azure-queue]`   | `azure-storage-queue` + `azure-identity`                    |
| `[azure-servicebus]` | `azure-servicebus` + `azure-identity`                    |

## Quick start — scheduler

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

`run_app` wires signal handling (SIGINT / SIGTERM), starts every service in
parallel, and exits cleanly when they all stop.

## Modules

| Module                                                                         | Status         | Purpose                                                 |
| ------------------------------------------------------------------------------ | -------------- | ------------------------------------------------------- |
| [`hosting`](localpost/hosting/)                                                | stable         | Service lifecycle, signals, middleware, ASGI/gRPC adapters |
| [`scheduler`](localpost/scheduler/)                                            | stable         | Composable in-process task scheduler                    |
| [`di`](localpost/di/)                                                          | stable         | Scoped IoC container                                    |
| [`http`](localpost/http/)                                                      | stable         | Small h11-based HTTP/1.1 server                         |
| [`experimental.consumers`](localpost/experimental/consumers/)                  | experimental   | Message broker consumer services                        |
| [`experimental.openapi`](localpost/experimental/openapi/)                      | experimental   | Type-driven OpenAPI framework                           |

"Stable" means the public API is not expected to break in a patch or minor
release. Experimental modules live under `localpost.experimental.<name>`;
the import path itself is the marker — the API is still being shaped and
breaking changes may land before `1.0`.

Each subdirectory has its own README with a quickstart, key concepts, and
extension points.

## Why localpost?

- **Type-safe** — public API is checked with `ty` and `basedpyright
  --verifytypes`.
- **FastAPI-style ergonomics** — decorators for tasks, services, and HTTP
  operations; declarative middleware.
- **Async-first** — built on AnyIO, so structured concurrency is the default,
  and you get Trio support for free.
- **Handle-both** — consumers and scheduler accept sync or async callables;
  sync ones are offloaded to a thread pool.
- **Small** — each module is focused and independently usable; take just the
  scheduler, just the hosting, or the full stack.

## Status

Beta — actively developed. Python 3.12+ required. See
[CHANGELOG.md](CHANGELOG.md) for history.

Examples for every module live under [`examples/`](examples/).

## License

MIT — see [LICENSE](LICENSE).

## Contributing

The `main` branch is the only stable branch and will never be force-pushed. Any
other branch (including release branches) may be rebased or force-pushed at any
time.

Dev setup:

```bash
just deps    # uv sync --all-groups --all-extras
just tests
```
