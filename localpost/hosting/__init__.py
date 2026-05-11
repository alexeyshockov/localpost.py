from typing import NoReturn

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


def run_app(*svcs: ServiceF) -> NoReturn:
    """Run the target app until it stops or is interrupted by a signal, then
    exit the process via :class:`SystemExit` with the resulting status code
    (``0`` on clean shutdown, ``1`` on failure).

    Intended as the program's ``__main__`` entrypoint — call directly,
    no ``sys.exit(...)`` wrapper needed.
    """

    root_svc = svcs[0] if len(svcs) == 1 else _run_many(*svcs)
    app = shutdown_on_signal()(root_svc)
    exit_code = anyio.run(run, app, None, **choose_anyio_backend())
    raise SystemExit(exit_code)
