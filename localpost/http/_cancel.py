"""HTTP-native request cancellation.

Independent of AnyIO and :mod:`localpost.threadtools` by design. Those layers
manage *worker* / *task* cancellation; this module manages *request*
cancellation — a distinct lifetime that ends when the HTTP client goes away
or when the hosted service is shutting down.
"""

from __future__ import annotations

import threading
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import final

__all__ = ["RequestCancelled", "RequestCancel", "check_cancelled"]


class RequestCancelled(Exception):
    """Raised by :func:`check_cancelled` when the current request is cancelled.

    Triggers:
      * The HTTP client disconnected mid-request (selector watchdog detected EOF).
      * The hosted service is shutting down.

    Inherits from :class:`Exception` (not :class:`BaseException`): request-scoped
    cancellation is meant to be catchable by ordinary ``except Exception:`` blocks.
    Worker / service cancellation lives on a separate, AnyIO-driven channel.
    """


@final
@dataclass(eq=False, slots=True)
class RequestCancel:
    """Per-request cancellation token. Flipped by the dispatcher on disconnect / shutdown."""

    _event: threading.Event = field(default_factory=threading.Event)

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()


_current: ContextVar[RequestCancel] = ContextVar("localpost.http._cancel.current")


def check_cancelled() -> None:
    """Cooperative cancellation check for HTTP request handlers.

    Raises:
        RequestCancelled: if the current request was cancelled.
        LookupError: if called outside an HTTP request context.
    """
    try:
        token = _current.get()
    except LookupError:
        raise LookupError("check_cancelled() called outside a request handler") from None
    if token.is_cancelled:
        raise RequestCancelled


@contextmanager
def _enter_request(token: RequestCancel) -> Generator[None]:
    """Bind ``token`` to the calling thread for the duration of the block."""
    reset = _current.set(token)
    try:
        yield
    finally:
        _current.reset(reset)
