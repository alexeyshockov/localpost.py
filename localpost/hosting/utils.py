import dataclasses as dc
from typing import TypeVar, final, Protocol

from anyio.streams.memory import MemoryObjectSendStream

from localpost.hosting import AbstractHost

T = TypeVar("T", contravariant=True)

__all__ = ["ThreadSafeSendStream", "ThreadSafeMemorySendStream"]


class ThreadSafeSendStream(Protocol[T]):
    def send_nowait(self, item: T) -> None: ...


@final
@dc.dataclass(frozen=True, slots=True)
class ThreadSafeMemorySendStream(ThreadSafeSendStream[T]):
    source: MemoryObjectSendStream[T]
    _host: AbstractHost

    def send_nowait(self, item: T) -> None:
        if self._host.same_thread:
            self.source.send_nowait(item)
        else:
            self._host.portal.start_task_soon(self.source.send_nowait, item).result()
