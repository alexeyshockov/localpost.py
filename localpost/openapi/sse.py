"""Server-Sent Events (SSE) primitives for ``localpost.openapi``.

Operations whose return type is a ``Generator[T, ...]`` /
``Iterator[T, ...]`` (or ``EventStream[T]``) are auto-promoted to an SSE
response: ``Content-Type: text/event-stream``, chunked transfer encoding,
one ``data:`` block per yielded value.

Yield :class:`Event` instances for full control over the SSE fields
(``event``, ``id``, ``retry``); yielding a bare value emits ``data: <json>``
only. Encoding follows the WHATWG SSE spec (newline-prefixed, double-newline
terminator).
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

import msgspec

if TYPE_CHECKING:
    from localpost.openapi.adapters import AdapterRegistry

__all__ = ["Event", "EventStream", "encode_event", "format_data_field", "iter_events"]


class Event[T](msgspec.Struct, eq=False, omit_defaults=True):
    """One SSE event.

    Args:
        data: Payload. Encoded as JSON unless it's already a ``str``.
        event: Optional event name (``event:`` field).
        id: Optional last event id (``id:`` field).
        retry: Optional reconnection delay in milliseconds (``retry:`` field).
        comment: Optional comment line. Useful for keep-alives — the spec says comment lines (``: ...``) are ignored
            by clients but keep the connection alive.
    """

    data: T
    event: str | None = None
    id: str | None = None
    retry: int | None = None
    comment: str | None = None


@dataclass(frozen=True, slots=True, eq=False)
class EventStream[T]:
    """Explicit SSE wrapper around a source iterator.

    Use when you want SSE behaviour without the framework auto-detecting
    a ``Generator`` return type — e.g. when returning from an iterator
    you've already constructed::

        @app.get("/feed")
        def feed() -> EventStream[Update]:
            return EventStream(updates_queue)

    For most cases just write a generator function and return the generator
    object directly; the framework recognises both shapes.
    """

    source: Iterable[T] | Iterable[Event[T]]


# --- Wire encoding -------------------------------------------------------


def format_data_field(payload: object, adapters: "AdapterRegistry | None" = None) -> str:
    """Encode an event's ``data`` value into the ``data:`` lines per the
    WHATWG SSE spec.

    A multi-line payload becomes one ``data:`` line per source line; a
    string is sent as-is; everything else is encoded via the matching
    :class:`TypeAdapter` from ``adapters`` (default registry if ``None``).
    """
    if isinstance(payload, str):
        text = payload
    elif isinstance(payload, (bytes, bytearray, memoryview)):
        text = bytes(payload).decode("utf-8")
    else:
        from localpost.openapi.adapters import default_registry  # noqa: PLC0415

        registry = adapters or default_registry()
        body, _ct = registry.for_value(payload).encode(payload)
        text = body.decode("utf-8")
    return "\n".join(f"data: {line}" for line in text.split("\n"))


def encode_event(event: Event[object] | object, adapters: "AdapterRegistry | None" = None) -> bytes:
    """Encode a single event (or bare payload) into SSE wire bytes.

    The output ends with the mandatory blank-line terminator (``\\n\\n``).
    """
    if isinstance(event, Event):
        lines: list[str] = []
        if event.comment is not None:
            lines.extend(f": {line}" for line in event.comment.split("\n"))
        if event.event is not None:
            lines.append(f"event: {event.event}")
        if event.id is not None:
            lines.append(f"id: {event.id}")
        if event.retry is not None:
            lines.append(f"retry: {event.retry}")
        lines.append(format_data_field(event.data, adapters))
        return ("\n".join(lines) + "\n\n").encode("utf-8")
    return (format_data_field(event, adapters) + "\n\n").encode("utf-8")


def iter_events(source: object, adapters: "AdapterRegistry | None" = None) -> Iterator[bytes]:
    """Drive ``source`` (a generator, iterator, or :class:`EventStream`)
    into a stream of SSE-encoded event bytes."""
    if isinstance(source, EventStream):
        source = source.source
    iterator: Iterator[object]
    if isinstance(source, Iterator):
        iterator = source
    elif isinstance(source, Iterable):
        iterator = iter(source)
    else:
        raise TypeError(f"SSE source must be iterable, got {type(source).__name__}")
    for item in iterator:
        yield encode_event(item, adapters)
