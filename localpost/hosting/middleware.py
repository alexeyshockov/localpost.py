from collections.abc import Awaitable, Callable
from functools import wraps

from anyio import move_on_after, open_signal_receiver

from localpost._utils import HANDLED_SIGNALS
from localpost.hosting._host import ServiceLifetime, ServiceLifetimeView, logger

ServiceF = Callable[[ServiceLifetime], Awaitable[None]]


async def _observe_started(lt: ServiceLifetimeView, timeout: float) -> None:
    with move_on_after(timeout):
        await lt.started
        return
    raise TimeoutError(f"Service did not start within {timeout} second(s)")


def start_timeout(timeout: float) -> Callable[[ServiceF], ServiceF]:
    def decorator(func: ServiceF) -> ServiceF:
        @wraps(func)
        def wrapper(lt: ServiceLifetime) -> Awaitable[None]:
            lt.tg.start_soon(lt.view.cancel_on_shutdown(_observe_started), lt, timeout)
            return func(lt)

        return wrapper

    return decorator


async def _handle_signals(h: ServiceLifetimeView, signals):
    with open_signal_receiver(*signals) as received:
        async for _ in received:
            # First Ctrl+C (or other termination method)
            if not h.shutting_down:
                logger.info("Shutting down...")
                h.shutdown()
                continue
            # Ctrl+C again
            logger.warning("Forced shutdown")
            h.stop()
            break


def shutdown_on_signal(*signals) -> Callable[[ServiceF], ServiceF]:
    def decorator(func: ServiceF) -> ServiceF:
        @wraps(func)
        def wrapper(lt: ServiceLifetime) -> Awaitable[None]:
            lt.tg.start_soon(_handle_signals, lt.view, signals or HANDLED_SIGNALS)
            return func(lt)

        return wrapper

    return decorator
