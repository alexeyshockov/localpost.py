"""Neutral HTTP message types — independent of any specific parser.

Both server backends (h11, httptools) populate the same shapes. User code
imports these from :mod:`localpost.http` and never touches a parser-specific
type directly.

Field shapes intentionally match h11's wire-level conventions:

- header names are lowercased ASCII bytes
- header values are bytes (server-side: ISO-8859-1 / ASCII per RFC 7230)
- ``http_version`` is the bare version (``b"1.1"``), not the prefixed form
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

__all__ = [
    "Request",
    "Response",
    "InformationalResponse",
    "BodyTooLarge",
]


@dataclass(frozen=True, slots=True, eq=False)
class Request:
    """Parsed HTTP request line + headers. Body is streamed via :meth:`HTTPReqCtx.receive`."""

    method: bytes
    """Uppercased ASCII method (e.g. ``b"GET"``). Both backends normalise here
    so consumers can compare against ``b"GET"`` / ``b"POST"`` / ... without
    case-folding per request."""
    target: bytes
    """Raw request-URI from the request line, including query string."""
    path: bytes
    """Path component of ``target`` (everything before ``?``). Pre-split by the
    backend so consumers don't redo the split on every request — the httptools
    backend uses ``httptools.parse_url`` (C-level), the h11 backend a manual
    ``split(b'?', 1)``."""
    query_string: bytes
    """Query string of ``target`` (everything after the first ``?``), or ``b""``
    if absent. Pre-split alongside :attr:`path`."""
    headers: Sequence[tuple[bytes, bytes]]
    """Header pairs in arrival order. Names are lowercased; values are as-sent."""
    http_version: bytes = b"1.1"
    """HTTP version as bare bytes (``b"1.1"`` or ``b"1.0"``)."""


@dataclass(frozen=True, slots=True, eq=False)
class Response:
    """Final response (2xx-5xx) — exactly one per request."""

    status_code: int
    headers: Sequence[tuple[bytes, bytes]] = field(default_factory=list)
    reason: bytes = b""
    """Reason phrase. Empty → backend supplies a default for the status code."""


@dataclass(frozen=True, slots=True, eq=False)
class InformationalResponse:
    """1xx response (100 Continue, 102 Processing, …). Multiple may precede the final response."""

    status_code: int
    headers: Sequence[tuple[bytes, bytes]] = field(default_factory=list)
    reason: bytes = b""
    """Reason phrase. Empty → backend supplies a default for the status code."""


class BodyTooLarge(Exception):
    """Raised when an incoming request body would exceed ``ServerConfig.max_body_size``.

    Surfaces both from ``HTTPReqCtx.receive`` (when the handler is reading the body)
    and from the connection's drain path (when the handler skipped reading it). The
    connection loop converts it into a 413 Payload Too Large response.
    """
