# localpost.hosting

Service lifecycle management and orchestration. A `service` is any async (or
sync) function wrapped with a lifecycle — it goes through `Starting →
Running → ShuttingDown → Stopped`, reacts to signals, and can spawn child
services in the same task group.

```python
import sys, time
from localpost.hosting import ServiceLifetime, run_app, service


@service
def my_service():
    def svc(lt: ServiceLifetime):
        lt.set_started()
        time.sleep(5)
    return svc


if __name__ == "__main__":
    sys.exit(run_app(my_service()))
```

`run_app()` wires `shutdown_on_signal()` (SIGINT / SIGTERM), runs services
under AnyIO (asyncio or Trio), and returns an exit code.

**Full reference:** <https://alexeyshockov.github.io/localpost.py/modules/hosting/>

Examples: [`examples/host/`](../../examples/host/).
