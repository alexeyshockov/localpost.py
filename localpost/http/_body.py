"""Request-body buffering helpers.

The server core never pre-buffers the request body — handlers that need
the whole body in memory call one of these helpers explicitly. They sit
on top of the public ``ctx.receive(size)`` Protocol method and enforce
``max_size`` while accumulating.

Sync (:func:`read_body`) and async (:func:`aread_body`) are siblings —
same contract, different I/O flavour.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from localpost.http._types import BodyTooLarge
from localpost.http.config import DEFAULT_BUFFER_SIZE

if TYPE_CHECKING:
    from localpost.http._async_base import AsyncHTTPReqCtx
    from localpost.http._base import HTTPReqCtx

__all__ = ["aread_body", "read_body"]


_DEFAULT_MAX_BODY_SIZE = 10 * 1024 * 1024  # 10 MiB — matches ServerConfig.max_body_size default.


def read_body(ctx: HTTPReqCtx, *, max_size: int = _DEFAULT_MAX_BODY_SIZE) -> bytes:
    """Read the full request body into a single :class:`bytes` object.

    Loops :meth:`HTTPReqCtx.receive` until EOF, accumulating into a
    ``bytearray`` and converting at the end. Raises :class:`BodyTooLarge`
    if the running total would exceed ``max_size`` (checked before each
    accumulating concat, so the helper never holds more than
    ``max_size + chunk_size`` bytes in memory).

    Sync handlers must hold a borrowed connection (or be wrapped by
    :func:`localpost.http.thread_pool_handler`) before calling — the
    selector loop is non-blocking and a synchronous body read needs the
    blocking-with-timeout socket mode that ``borrow()`` arranges.
    """
    buf = bytearray()
    while True:
        chunk = ctx.receive(DEFAULT_BUFFER_SIZE)
        if not chunk:
            return bytes(buf)
        if len(buf) + len(chunk) > max_size:
            raise BodyTooLarge(len(buf) + len(chunk))
        buf += chunk


async def aread_body(ctx: AsyncHTTPReqCtx, *, max_size: int = _DEFAULT_MAX_BODY_SIZE) -> bytes:
    """Async sibling of :func:`read_body`.

    Same contract: drain ``await ctx.receive(size)`` until EOF, raising
    :class:`BodyTooLarge` if the cap is exceeded mid-stream.
    """
    buf = bytearray()
    while True:
        chunk = await ctx.receive(DEFAULT_BUFFER_SIZE)
        if not chunk:
            return bytes(buf)
        if len(buf) + len(chunk) > max_size:
            raise BodyTooLarge(len(buf) + len(chunk))
        buf += chunk
