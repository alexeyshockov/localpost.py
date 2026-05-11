# localpost.di

Small, `.NET`-style inversion-of-control container: scoped service resolution
with automatic constructor wiring. No decorators, no async, no "one interface,
many implementations" — just a registry + provider + scope. Comes with core
`localpost`; the optional Flask adapter requires `flask` in your environment.

```python
from dataclasses import dataclass
from localpost.di._services import ServiceRegistry


@dataclass
class Config: host: str; port: int


class Server:
    def __init__(self, config: Config): self.config = config


services = ServiceRegistry()
services.register_instance(Config(host="127.0.0.1", port=8080))
services.register(Server)  # auto-wires Config

with services.app_scope() as sp:
    print(sp.resolve(Server).config)
```

**Full reference:** <https://alexeyshockov.github.io/localpost.py/modules/di/>

Examples: [`examples/di/`](../../examples/di/).
