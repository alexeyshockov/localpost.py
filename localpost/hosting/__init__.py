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
    _run_many,
    current_service,
    observe_services,
    run,
    serve,
    service,
)
from .middleware import shutdown_on_signal
from .rsgi import HostRSGIApp

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
    "HostRSGIApp",
]


def run_app(*svcs: ServiceF) -> int:
    """Run the target app until it stops or is interrupted by a signal."""

    root_svc = svcs[0] if len(svcs) == 1 else _run_many(*svcs)
    app = shutdown_on_signal()(root_svc)
    return anyio.run(run, app, None, **choose_anyio_backend())
