"""Async SSE drive — mirror of :mod:`localpost.openapi.sse`'s
:func:`iter_events`.

The wire-format helpers (:class:`Event`, :func:`encode_event`,
:func:`format_data_field`) live in :mod:`localpost.openapi.sse` and are
shared between sync and async runtimes. Only the iteration over the
source stream is async-specific.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, cast

from localpost.openapi.sse import EventStream, encode_event

if TYPE_CHECKING:
    from localpost.openapi.adapters import AdapterRegistry

__all__ = ["async_iter_events"]


async def async_iter_events(
    source: object,
    adapters: AdapterRegistry | None = None,
) -> AsyncIterator[bytes]:
    """Drive ``source`` (an async generator, async iterator, or
    :class:`EventStream` whose ``.source`` is itself async-iterable) into
    a stream of SSE-encoded event bytes.

    Sync iterators are explicitly rejected — the async runtime won't
    silently bridge a blocking generator onto the event loop.
    """
    if isinstance(source, EventStream):
        source = source.source
    if not hasattr(source, "__aiter__"):
        raise TypeError(
            f"AsyncOperation: SSE source must be an async iterator/generator, got {type(source).__name__}; "
            f"use ``async def`` generators in HttpAsyncApp handlers"
        )
    async for item in cast(AsyncIterator[Any], source):
        yield encode_event(item, adapters)
