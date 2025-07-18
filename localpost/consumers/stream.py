import math
from collections.abc import Callable
from typing import Generic, TypeVar

from anyio.abc import ObjectReceiveStream

from localpost._utils import wait_any
from localpost.flow import AnyHandlerManager, ensure_async_handler
from localpost.flow._stream import create_stream_consumer
from localpost.hosting import ServiceLifetimeManager

T = TypeVar("T")

__all__ = ["stream_consumer"]


class StreamConsumerService(Generic[T]):
    def __init__(
        self,
        target: ObjectReceiveStream[T],
        handler: AnyHandlerManager[T],
        /,
        *,
        concurrency: int,
        process_leftovers: bool,
    ):
        if not (math.isinf(concurrency) or (isinstance(concurrency, int) and concurrency >= 1)):
            raise ValueError("Number of consumers must be at least 1")

        self.target = target
        self.handler_m = handler
        self.concurrency = concurrency
        self.process_leftovers = process_leftovers

    async def __call__(self, service_lifetime: ServiceLifetimeManager):
        receiver = self.target
        async with (
            receiver,
            self.handler_m as handler,
            create_stream_consumer(
                receiver,
                ensure_async_handler(handler, max_threads=self.concurrency),
                concurrency=self.concurrency,
            ) as consumer_state,
        ):
            service_lifetime.set_started()
            if not self.process_leftovers:
                await wait_any(service_lifetime.shutting_down, consumer_state.closed)
                await receiver.aclose()
            # Otherwise just wait for the stream to be exhausted and closed


def stream_consumer(
    target: ObjectReceiveStream[T],
    /,
    *,
    concurrency: int = 1,
    process_leftovers: bool = True,
) -> Callable[[AnyHandlerManager[T]], StreamConsumerService[T]]:
    """Decorator to create a stream consumer hosted service."""
    return lambda handler_m: StreamConsumerService(
        target, handler_m, concurrency=concurrency, process_leftovers=process_leftovers
    )
