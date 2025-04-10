import logging
import threading

import anyio
from anyio import open_signal_receiver

from ._utils import HANDLED_SIGNALS, cancellable_from, choose_anyio_backend
from .hosting import AbstractHost, Host, HostedServiceFunc

logger = logging.getLogger("localpost")


def _ensure_host(target: AbstractHost | HostedServiceFunc) -> AbstractHost:
    if isinstance(target, AbstractHost):
        return target
    return Host(target)


def run(target: AbstractHost | HostedServiceFunc) -> int:
    """
    Run the target host (or service) until it stops or is interrupted by a signal.
    """
    return anyio.run(arun, target, **choose_anyio_backend())


async def arun(target: AbstractHost | HostedServiceFunc) -> int:
    """
    Run the target host (or service) until it stops or is interrupted by a signal.
    """
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Signals can only be installed on the main thread")

    host = _ensure_host(target)

    @cancellable_from(host.stopped)
    async def handle_signals():
        with open_signal_receiver(*HANDLED_SIGNALS) as signals:
            async for _ in signals:
                if not host.shutting_down:  # First Ctrl+C (or other termination method)
                    logger.info("Shutting down...")
                    host.shutdown()
                    continue
                # Ctrl+C again
                logger.warning("Forced shutdown")
                host.stop()
                break

    async with host.aserve():
        await handle_signals()

    return host.exit_code
