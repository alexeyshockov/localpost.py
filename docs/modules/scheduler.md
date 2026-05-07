# localpost.scheduler

Composable in-process task scheduler. Tasks are triggered by **conditions**
(time intervals, cron expressions, completion of another task), and triggers
are built up with operators — `//` to compose middleware, `>>` to extend a
trigger's own middleware pipeline. Tasks publish their outputs as `Result[T]`,
so downstream tasks can subscribe.

Use it when you need to schedule background work inside the same process — for
example, refresh a cached dataframe every minute. For distributed /
persistent tasks, use Celery, APScheduler with a DB store, etc.

## Install

```bash
pip install localpost[scheduler]          # human-readable periods (pytimeparse2, humanize)
pip install localpost[scheduler,cron]     # also the cron() trigger (croniter)
```

## Quick start

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

Cron:

```python
from localpost.scheduler import delay, scheduled_task
from localpost.scheduler.cond.cron import cron


@scheduled_task(cron("*/1 * * * *") // delay((0, 10)))
async def job():
    print("running")
```

See [`examples/scheduler/`](https://github.com/alexeyshockov/localpost.py/tree/main/examples/scheduler/)
for more (`cancellation.py`, `failing_tasks.py`, `fastapi_lifespan.py`,
`finite_task.py`, `serve_sync.py`, `fastdepends.py`).

## Key concepts

- **`ScheduledTask[T, R]`** — the runtime object: a handler paired with a
  trigger factory. Publishes a `Result[R]` stream that others can subscribe to.
- **`ScheduledTaskTemplate[T]`** — a pre-bound trigger factory waiting for a
  handler. Composable with `//` (compose trigger middleware) and `>>`
  (extend the trigger's own middleware tuple). `every(...)`, `after(...)`,
  `after_all(...)`, and `cron(...)` all return templates.
- **`TriggerFactory[T]`** — `Callable[[TaskGroup, EventView],
  AbstractAsyncContextManager[AsyncIterator[T]]]`. Under the hood, a trigger is
  an async iterator of events fired inside the task's lifetime.
- **Trigger middleware** — an async generator that consumes one `AsyncIterator`
  and yields another. `delay` and `take_first` are built-in; write your own the
  same way.
- **`Result[T]`** — output envelope (`Ok` or failure). `after()` filters
  successes and forwards the `T`; `after_all()` forwards every result.
- **Hosting integration** — both individual tasks and `Scheduler` instances
  are `ServiceF`s. Pass them to `localpost.hosting.run_app(...)` (entry point,
  signal handling) or `localpost.hosting.serve(...)` (async CM, e.g. for
  embedding in a FastAPI lifespan).

## Writing a custom trigger

A trigger is a frozen dataclass / callable that, given a `TaskGroup` and a
`shutting_down` event, returns an async context manager yielding an
`AsyncIterator[T]`. Pattern (adapted from `_cond.py:Every`):

```python
from dataclasses import dataclass, replace
from contextlib import asynccontextmanager

@dataclass(frozen=True, slots=True)
class MyTrigger[T]:
    middlewares: tuple[TriggerMiddleware, ...] = ()

    def __rshift__(self, mw):  # >> adds a middleware
        return replace(self, middlewares=self.middlewares + (mw,))

    @asynccontextmanager
    async def __call__(self, tg, shutting_down):
        async with apply_middlewares(my_source(), self.middlewares) as stream:
            yield stream
```

Then wrap it: `my_trigger = ScheduledTaskTemplate(MyTrigger())`.

## Writing a custom middleware

A middleware is a regular async generator:

```python
from collections.abc import AsyncIterator
from localpost._utils import maybe_closing

def skip_every_other():
    async def middleware[T](events: AsyncIterator[T]) -> AsyncIterator[T]:
        async with maybe_closing(events):
            i = 0
            async for event in events:
                if i % 2 == 0:
                    yield event
                i += 1
    return middleware
```

Use it via `every("1s") // skip_every_other()`.

## See also

- Examples: [`examples/scheduler/`](https://github.com/alexeyshockov/localpost.py/tree/main/examples/scheduler/)
- Cron source: [`cond/cron.py`](https://github.com/alexeyshockov/localpost.py/blob/main/localpost/scheduler/cond/cron.py)
- Built-in triggers: [`_cond.py`](https://github.com/alexeyshockov/localpost.py/blob/main/localpost/scheduler/_cond.py)
- Core: [`_scheduler.py`](https://github.com/alexeyshockov/localpost.py/blob/main/localpost/scheduler/_scheduler.py)
