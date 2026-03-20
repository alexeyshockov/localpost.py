import anyio

from .._utils import choose_anyio_backend
from ._host import (
    Running,
    ServiceF,
    ServiceLifetime,
    ServiceLifetimeView,
    ServiceState,
    ShuttingDown,
    Starting,
    Stopped,
    current_service,
    observe_services,
    run,
    serve,
    service,
)
from .middleware import shutdown_on_signal

__all__ = [
    "service",
    "observe_services",
    "current_service",
    "serve",
    "run",
    "run_app",
    "ServiceState",
    "Starting",
    "Running",
    "ShuttingDown",
    "ServiceLifetime",
    "ServiceLifetimeView",
    "Stopped",
]


def run_app(svc: ServiceF, /) -> int:
    """Run the target app until it stops or is interrupted by a signal."""

    app = shutdown_on_signal()(svc)
    return anyio.run(run, app, None, **choose_anyio_backend())
