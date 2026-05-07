# localpost.scheduler

Composable in-process task scheduler. Tasks are triggered by **conditions**
(time intervals, cron expressions, completion of another task), and triggers
are built up with operators — `//` to compose middleware, `>>` to extend a
trigger's own middleware pipeline. Tasks publish `Result[T]` so downstream
tasks can subscribe.

```bash
pip install localpost[scheduler]          # human-readable periods
pip install localpost[scheduler,cron]     # also the cron() trigger
```

```python
import sys, random
from localpost.hosting import run_app
from localpost.scheduler import every, scheduled_task


@scheduled_task(every("3s"))
async def task1():
    return random.randint(1, 22)


if __name__ == "__main__":
    sys.exit(run_app(task1))
```

**Full reference:** <https://alexeyshockov.github.io/localpost.py/modules/scheduler/>

Examples: [`examples/scheduler/`](../../examples/scheduler/).
