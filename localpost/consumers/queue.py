import logging
from collections.abc import Awaitable, Callable
from queue import Queue, SimpleQueue
from typing import Generic

from anyio import CapacityLimiter, ClosedResourceError, create_task_group, to_thread
from typing_extensions import TypeVar

from localpost._utils import Event, wait_any
from localpost.flow import AnyHandlerManager, ensure_sync_handler
from localpost.flow._queue import Receiver, ShutDown
from localpost.hosting import ServiceLifetimeManager

T = TypeVar("T")

__all__ = ["queue_consumer"]

logger = logging.getLogger(__name__)


class QueueConsumerService(Generic[T]):
    def __init__(
        self,
        target: Queue[T] | SimpleQueue[T],
        handler_m: AnyHandlerManager[T],
        /,
        *,
        consumers: int,
        process_leftovers: bool,
        task_tracking: bool | None,
    ):
        if not isinstance(consumers, int) or consumers < 1:
            raise ValueError("Number of consumers must be at least 1")

        self.target = target
        self.handler_m = handler_m
        self.num_consumers = consumers
        self.process_leftovers = process_leftovers
        self.task_tracking = hasattr(target, "task_done") if task_tracking is None else task_tracking

    async def __call__(self, service_lifetime: ServiceLifetimeManager):
        def source_items():
            try:
                while True:
                    yield stream.receive()
            except ShutDown:
                logger.debug("Source queue has been completed, no more items to process")
            except ClosedResourceError:
                logger.debug("Receiver has been closed (according to consumer's process_leftovers setting)")

        def consume() -> None:
            for item in source_items():
                handle(item)
                if self.task_tracking:
                    self.target.task_done()  # type: ignore

        def consumer_thread() -> Awaitable[None]:
            return to_thread.run_sync(consume, limiter=threads_limiter)

        closed = Event()
        stream = Receiver(self.target)
        threads_limiter = CapacityLimiter(self.num_consumers)
        async with self.handler_m as handler, create_task_group() as consumers_tg:
            handle = ensure_sync_handler(handler)
            for _ in range(self.num_consumers):
                consumers_tg.start_soon(consumer_thread)
            service_lifetime.set_started()
            # TODO Timeout, if the queue is not closed at all
            if not self.process_leftovers:
                await wait_any(service_lifetime.shutting_down, closed)
                stream.close()
            # Otherwise just wait for the stream to be exhausted and closed


def queue_consumer(
    target: Queue[T] | SimpleQueue[T],
    /,
    *,
    consumers: int = 1,
    process_leftovers: bool = True,
    task_tracking: bool | None = None,
) -> Callable[[AnyHandlerManager[T]], QueueConsumerService[T]]:
    """Decorator to create a Queue/SimpleQueue consumer hosted service."""
    return lambda handler_m: QueueConsumerService(
        target, handler_m, consumers=consumers, process_leftovers=process_leftovers, task_tracking=task_tracking
    )
