"""HTTP-native request cancellation.

Independent of AnyIO and :mod:`localpost.threadtools` by design. Those layers
manage *worker* / *task* cancellation; this module manages *request*
cancellation — a distinct lifetime that ends when the HTTP client goes away
or when the hosted service is shutting down.
"""

from __future__ import annotations

import socket
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
      * The HTTP client disconnected mid-request (detected by a non-blocking
        ``MSG_PEEK`` on the request socket).
      * The hosted service is shutting down.

    Inherits from :class:`Exception` (not :class:`BaseException`): request-scoped
    cancellation is meant to be catchable by ordinary ``except Exception:`` blocks.
    Worker / service cancellation lives on a separate, AnyIO-driven channel.
    """


@final
@dataclass(eq=False, slots=True)
class RequestCancel:
    """Per-request cancellation token.

    Three signals collapsed into one:

    * Explicit :meth:`cancel` (sets ``_event``) — used by tests and any future
      per-request timeout.
    * Service shutdown — a single shared :class:`threading.Event` is OR-ed in.
      Set once when the hosted service is winding down; every in-flight token
      sees it without a registry.
    * Client disconnect — :meth:`is_cancelled` does a non-blocking
      ``recv(1, MSG_PEEK | MSG_DONTWAIT)`` on the request socket. ``b""`` means
      EOF (peer FIN); ``BlockingIOError`` means no signal; any other ``OSError``
      treats the connection as broken. Once detected, the result is cached on
      ``_event`` so subsequent reads don't re-issue the syscall.

    ``MSG_PEEK`` is safe alongside the worker's send/recv on the same socket:
    peek doesn't consume bytes, and the read and write paths are independent at
    the kernel level.
    """

    _sock: socket.socket
    _shutdown_event: threading.Event
    _event: threading.Event = field(default_factory=threading.Event)

    def cancel(self) -> None:
        self._event.set()

    @property
    def fired(self) -> bool:
        """``True`` iff cancellation was already signalled — explicit cancel,
        service shutdown, or a previous PEEK detected disconnect (cached on
        ``_event``).

        Cheap (no syscall). For the cooperative ``check_cancelled`` path that
        actively probes the socket for client disconnect, use :attr:`is_cancelled`.
        """
        return self._event.is_set() or self._shutdown_event.is_set()

    @property
    def is_cancelled(self) -> bool:
        if self.fired:
            return True
        try:
            peeked = self._sock.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
        except BlockingIOError:
            return False
        except OSError:
            self._event.set()
            return True
        if not peeked:
            self._event.set()
            return True
        return False


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
