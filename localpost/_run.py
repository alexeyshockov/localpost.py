import logging
import threading

import anyio
from anyio import open_signal_receiver

from ._utils import HANDLED_SIGNALS, cancellable_from, choose_anyio_backend
from .hosting import Host
from .scheduler import Scheduler

logger = logging.getLogger("localpost")


def run(target: Host | Scheduler) -> int:
    return anyio.run(arun, target, **choose_anyio_backend())


async def arun(target: Host | Scheduler) -> int:
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Signals can only be installed on the main thread")

    host = target if isinstance(target, Host) else Host(target, name=f"{target.name}_host")

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
