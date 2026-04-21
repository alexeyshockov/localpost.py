# localpost.di

> **Status:** stable — public API is not expected to break in patch/minor releases.

Small, `.NET`-style inversion-of-control container: scoped service resolution
with automatic constructor wiring. No decorators, no async, no "one interface,
many implementations" — just a registry + provider + scope.

## Install

Comes with core `localpost` — no extra needed. The Flask adapter requires
`flask` in your environment.

## Quick start

```python
from dataclasses import dataclass
from localpost.di._services import ServiceRegistry


@dataclass
class Config:
    host: str
    port: int


class Server:
    def __init__(self, config: Config):
        self.config = config


services = ServiceRegistry()
services.register_instance(Config(host="127.0.0.1", port=8080))
services.register(Server)                  # auto-wires Config via __init__

with services.app_scope() as sp:
    server = sp.resolve(Server)
    print(server.config)
```

With cleanup (generator factory):

```python
def create_db_pool(conf: Config):
    pool = DBPool(conf.db_dsn)
    try:
        yield pool
    finally:
        pool.close()


services.register(DBPool, create_db_pool)
```

See [`examples/di/basic.py`](../../examples/di/basic.py),
[`basic_cleanup.py`](../../examples/di/basic_cleanup.py),
[`flask_app.py`](../../examples/di/flask_app.py).

## Design goals (and non-goals)

- Inspired by .NET `Microsoft.Extensions.DependencyInjection`.
- No `async` (for now).
- **Scope is defined by its type** (`AppContext`, `RequestContext`, …).
- **Only one registration per `(service_type, scope_type)` pair** (the opposite
  of .NET's `IEnumerable<TService>` pattern). A type can still be registered
  multiple times — once per scope.
- **Service Locator** style — no magic `@inject` decorator. Ask the provider
  for what you need.
- Constructor dependencies are **auto-wired** from type hints when resolved
  via the provider.
- Factories can be plain callables, or **generator functions** for
  setup/teardown (enter on resolve, `finally` on scope exit).
- Instances are **lazily resolved** by default; use `create_on_enter=True` to
  build them eagerly when a scope is entered.

## Key concepts

- **`ResolutionContext`** (Protocol) — anything with `enter(cm)`. `AppContext`
  is the default app-wide scope; `RequestContext` (in the Flask adapter) is
  per-request. Define your own for other lifetimes.
- **`ServiceRegistry`** — the mutable catalogue. Holds `ServiceDescriptor`s
  keyed by `(service_type, scope_type)`.
- **`ServiceProvider`** — resolves services. The default provider walks the
  scope chain (request → app) to find a descriptor.
- **Scope stack** — child scopes hold a reference to their parent provider, so
  resolving a parent-scoped service from inside a child scope transparently
  reuses it.
- **`service_provider` proxy** — a `CurrentServiceProvider` singleton that
  delegates to whichever provider is active in the current `contextvar`.
  Lets request-scoped code call `service_provider.resolve(...)` without
  plumbing the provider through.

## Public API

All public symbols live under `localpost.di._services` (module layout is
being stabilised; expect a top-level `__init__` re-export):

| Symbol                                          | Notes                                    |
| ----------------------------------------------- | ---------------------------------------- |
| `ServiceRegistry()`                             | Mutable registry of descriptors          |
| `registry.register_instance(value, service_type=None, scope=None)` | Bind an already-built value |
| `registry.register(service_type, factory=None, scope=None, create_on_enter=False)` | Register a type / factory |
| `registry.app_scope()`                          | Context manager yielding a `ServiceProvider` |
| `ServiceProvider` (Protocol)                    | `resolve[T](type)`, `sp[Type]`, `create[T](type, **kw)` |
| `AppContext`                                    | Default scope type                       |
| `service_provider`                              | `CurrentServiceProvider` proxy (contextvar-backed) |
| `ServiceDescriptor`                             | Frozen (service_type, scope_type, factory) tuple |
| `ResolutionContext` (Protocol)                  | Implement for custom scopes              |
| `ServiceNotRegisteredError`, `NoResolutionContextError` | Exceptions                       |

### Flask adapter — `localpost.di.flask`

| Symbol                                    | Notes                                        |
| ----------------------------------------- | -------------------------------------------- |
| `RequestContext`                          | Per-request scope                            |
| `init_app(app, registry, provider)`       | Opens a `RequestContext` per Flask request; registers `flask.Request` for auto-injection |

A Quart adapter (`localpost/di/quart.py`) exists as a stub only.

## Defining a custom scope

1. Create a dataclass implementing `ResolutionContext`:

   ```python
   @dataclass(frozen=True, eq=False, slots=True)
   class JobContext:
       ctx: ExitStack = field(default_factory=ExitStack)
       def enter[T](self, cm): return self.ctx.enter_context(cm)
   ```

2. Register services under it: `services.register(JobRepo, scope=JobContext)`.

3. Open the scope when you start a job:

   ```python
   from localpost.di._services import DefaultServiceProvider, scope
   job_ctx = JobContext()
   provider = DefaultServiceProvider(parent_provider, registry, job_ctx, JobContext)
   with job_ctx.ctx, scope(provider):
       run_job()
   ```

The Flask adapter in [`flask.py`](flask.py) is a compact reference.

## See also

- Examples: [`examples/di/`](../../examples/di/)
- Core: [`_services.py`](_services.py)
