"""Static file serving via ``socket.sendfile()``.

Serves files from a root directory with zero-copy bodies, conditional GET
(``ETag`` / ``If-None-Match``, ``Last-Modified`` / ``If-Modified-Since``),
single-range support, and ``Cache-Control`` passthrough. Designed to be
wrapped in :func:`localpost.http.thread_pool_handler` with its own
concurrency budget so slow clients can't pin API workers.

Pre-body decisions (404 / 405 / 304 / 416, plus HEAD success) run inline
on the selector via :meth:`HTTPReqCtx.complete`. GET 200 / 206 returns a
:data:`BodyHandler` that opens the file and ``sendfile`` s it on the
worker thread.
"""

from __future__ import annotations

import mimetypes
import os
from collections.abc import Mapping
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path
from stat import S_ISREG
from typing import Final, Literal
from urllib.parse import unquote_to_bytes

from localpost.http._base import BodyHandler, HTTPReqCtx, RequestHandler
from localpost.http._types import Response

__all__ = ["static_handler"]

_ALLOW_METHODS: Final = b"GET, HEAD"
_NOT_FOUND_BODY: Final = b"Not Found"
_METHOD_NOT_ALLOWED_BODY: Final = b"Method Not Allowed"
_RANGE_NOT_SATISFIABLE_BODY: Final = b"Range Not Satisfiable"


def static_handler(
    root: str | os.PathLike[str],
    *,
    prefix: bytes = b"/",
    cache_control: str | None = None,
    index: str | None = "index.html",
) -> RequestHandler:
    """Build a :data:`RequestHandler` that serves files under ``root``.

    Args:
        root: Directory to serve from. Resolved at construction; every
            request path is verified to stay under this directory.
        prefix: URL prefix to strip from ``ctx.request.path`` before
            resolving against ``root``. Default ``b"/"`` matches a
            handler mounted at the URL root.
        cache_control: If set, the value is sent verbatim as the
            ``Cache-Control`` response header on 200 / 206 / 304.
        index: Filename to serve when the resolved path is a directory.
            ``None`` disables directory → index resolution (returns 404).

    Returns:
        A :data:`RequestHandler`. For 200 / 206 GET, returns a
        :data:`BodyHandler` so a wrapping :func:`thread_pool_handler`
        can dispatch the ``sendfile`` to a worker. Other outcomes
        complete inline on the selector and return ``None``.

    Example::

        from localpost.http import http_server, thread_pool_handler
        from localpost.http.static import static_handler

        h = thread_pool_handler(
            static_handler("/var/www", prefix=b"/static/",
                           cache_control="public, max-age=31536000, immutable"),
        )
    """
    root_path = Path(os.fspath(root)).resolve(strict=True)
    if not root_path.is_dir():
        raise ValueError(f"root must be a directory: {root_path}")
    cc_bytes = cache_control.encode("ascii") if cache_control is not None else None

    def handle(ctx: HTTPReqCtx) -> BodyHandler | None:
        method = ctx.request.method
        if method not in (b"GET", b"HEAD"):
            # Always include the body — the caller used a non-HEAD method.
            ctx.complete(
                Response(
                    status_code=405,
                    headers=[
                        (b"allow", _ALLOW_METHODS),
                        (b"content-type", b"text/plain; charset=utf-8"),
                        (b"content-length", str(len(_METHOD_NOT_ALLOWED_BODY)).encode("ascii")),
                    ],
                ),
                _METHOD_NOT_ALLOWED_BODY,
            )
            return None

        url_path = ctx.request.path
        if not url_path.startswith(prefix):
            _send_not_found(ctx, method)
            return None

        target = _resolve(root_path, url_path[len(prefix):], index=index)
        if target is None:
            _send_not_found(ctx, method)
            return None

        try:
            st = target.stat()
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            _send_not_found(ctx, method)
            return None
        if not S_ISREG(st.st_mode):
            _send_not_found(ctx, method)
            return None

        size = st.st_size
        etag = _etag(st)
        last_modified = formatdate(st.st_mtime, usegmt=True).encode("ascii")
        headers = _headers_index(ctx.request.headers)

        # Conditional GET — If-None-Match takes precedence over If-Modified-Since.
        inm = headers.get(b"if-none-match")
        ims = headers.get(b"if-modified-since")
        if inm is not None and _if_none_match_matches(inm, etag):
            _send_not_modified(ctx, etag, last_modified, cc_bytes)
            return None
        if inm is None and ims is not None and _if_modified_since_satisfied(ims, st.st_mtime):
            _send_not_modified(ctx, etag, last_modified, cc_bytes)
            return None

        # Range — single byte-range only. Multi-range / unparseable falls back to 200.
        offset = 0
        length = size
        status = 200
        content_range: bytes | None = None
        if (rng := headers.get(b"range")) is not None:
            parsed = _parse_range(rng, size)
            if parsed == "unsatisfiable":
                _send_range_not_satisfiable(ctx, method, size, etag, last_modified)
                return None
            if parsed is not None:
                offset, length = parsed
                status = 206
                content_range = f"bytes {offset}-{offset + length - 1}/{size}".encode("ascii")

        response_headers: list[tuple[bytes, bytes]] = [
            (b"content-type", _content_type(target)),
            (b"content-length", str(length).encode("ascii")),
            (b"etag", etag),
            (b"last-modified", last_modified),
            (b"accept-ranges", b"bytes"),
        ]
        if content_range is not None:
            response_headers.append((b"content-range", content_range))
        if cc_bytes is not None:
            response_headers.append((b"cache-control", cc_bytes))

        response = Response(status_code=status, headers=response_headers)

        if method == b"HEAD" or length == 0:
            ctx.complete(response)
            return None

        return _SendFileBody(target, response, offset, length)

    return handle


# --------------------------------------------------------------------------
# Body handler — sendfile on the worker thread
# --------------------------------------------------------------------------


class _SendFileBody:
    """Body handler closure: open + :meth:`HTTPReqCtx.sendfile` + finish.

    Spelled as a callable class (not a closure) so its captured state is
    visible in repr — same convention as the dispatch-chain callables in
    :mod:`localpost.http._base`.
    """

    __slots__ = ("length", "offset", "path", "response")

    def __init__(self, path: Path, response: Response, offset: int, length: int) -> None:
        self.path = path
        self.response = response
        self.offset = offset
        self.length = length

    def __call__(self, ctx: HTTPReqCtx) -> None:
        with self.path.open("rb") as f:
            ctx.sendfile(self.response, f, self.offset, self.length)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _resolve(root: Path, rel: bytes, *, index: str | None) -> Path | None:
    """Decode ``rel`` and resolve under ``root``. Returns ``None`` on
    decode failure, traversal, or a directory request without an
    ``index``. The returned path may not exist — caller stats it.
    """
    try:
        decoded = unquote_to_bytes(rel).decode("utf-8")
    except UnicodeDecodeError:
        return None
    if "\0" in decoded:
        return None
    # Defensive: reject ``..`` segments before path resolution. ``Path.resolve``
    # would normalise them out, but we'd still want to refuse the request
    # rather than silently serve a sibling.
    if any(p == ".." for p in decoded.split("/")):
        return None
    candidate = (root / decoded.lstrip("/")).resolve()
    if not candidate.is_relative_to(root):
        return None
    # Directory → append index, re-resolve so the same containment check
    # applies. ``index`` is a config-supplied filename, so it can't escape,
    # but the re-resolution is cheap and keeps the invariant explicit.
    try:
        if candidate.is_dir():
            if index is None:
                return None
            candidate = (candidate / index).resolve()
            if not candidate.is_relative_to(root):
                return None
    except OSError:
        return None
    return candidate


def _etag(st: os.stat_result) -> bytes:
    """Strong ETag from inode size + mtime nanoseconds.

    Cheap and stable across restarts — no need for a content hash.
    """
    return f'"{st.st_size:x}-{st.st_mtime_ns:x}"'.encode("ascii")


def _if_none_match_matches(header: bytes, etag: bytes) -> bool:
    """RFC 7232 §3.2: weak comparison. ``W/"x"`` matches ``"x"`` and vice
    versa; both compare equal to our strong tag.
    """
    if header.strip() == b"*":
        return True
    bare = etag[2:] if etag.startswith(b"W/") else etag
    for tag in header.split(b","):
        tag = tag.strip()
        if tag.startswith(b"W/"):
            tag = tag[2:]
        if tag == bare:
            return True
    return False


def _if_modified_since_satisfied(header: bytes, mtime: float) -> bool:
    """Return ``True`` iff the resource has *not* been modified since the
    header date — i.e. caller should respond 304.
    """
    try:
        since = parsedate_to_datetime(header.decode("ascii"))
    except (UnicodeDecodeError, TypeError, ValueError):
        return False
    if since is None:
        return False
    # HTTP-date carries whole-second precision; compare on integer seconds.
    return int(mtime) <= int(since.timestamp())


def _parse_range(header: bytes, size: int) -> tuple[int, int] | Literal["unsatisfiable"] | None:
    """Parse a ``Range:`` header. Returns ``(offset, length)`` for a
    satisfiable single byte-range, ``"unsatisfiable"`` for a syntactically
    valid but out-of-bounds range (→ 416), or ``None`` for absent /
    unparseable / multi-range (→ fall back to full body).
    """
    unit, _, spec = header.strip().partition(b"=")
    if unit.lower() != b"bytes" or not spec:
        return None
    if b"," in spec:
        # Multi-range. RFC 7233 allows it; our writer doesn't speak
        # multipart/byteranges. Falling back to 200 is RFC-compliant.
        return None
    start_b, sep, end_b = spec.partition(b"-")
    if not sep:
        return None
    if size == 0:
        # Any range against an empty file is unsatisfiable per RFC 7233 §2.1.
        return "unsatisfiable"
    try:
        if not start_b:
            # Suffix range: last N bytes.
            if not end_b:
                return None
            n = int(end_b)
            if n <= 0:
                return None
            n = min(n, size)
            return size - n, n
        start = int(start_b)
        if start < 0:
            return None
        if start >= size:
            return "unsatisfiable"
        if not end_b:
            return start, size - start
        end = int(end_b)
        if end < start:
            return None
        end = min(end, size - 1)
        return start, end - start + 1
    except ValueError:
        return None


def _content_type(path: Path) -> bytes:
    mime, _ = mimetypes.guess_type(path.name)
    return (mime or "application/octet-stream").encode("ascii")


def _headers_index(headers) -> Mapping[bytes, bytes]:
    """Last-value-wins lookup over header pairs. Sufficient for the
    request headers we read here (``If-None-Match``, ``If-Modified-Since``,
    ``Range``) — none of them are list-valued in practice.
    """
    return dict(headers)


# --------------------------------------------------------------------------
# Canned non-success responses (selector-thread inline)
# --------------------------------------------------------------------------


def _send_not_found(ctx: HTTPReqCtx, method: bytes) -> None:
    ctx.complete(
        Response(
            status_code=404,
            headers=[
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(_NOT_FOUND_BODY)).encode("ascii")),
            ],
        ),
        # HEAD: headers describe the body that GET would receive; the bytes
        # themselves must not go on the wire.
        None if method == b"HEAD" else _NOT_FOUND_BODY,
    )


def _send_not_modified(
    ctx: HTTPReqCtx,
    etag: bytes,
    last_modified: bytes,
    cache_control: bytes | None,
) -> None:
    headers: list[tuple[bytes, bytes]] = [
        (b"etag", etag),
        (b"last-modified", last_modified),
    ]
    if cache_control is not None:
        headers.append((b"cache-control", cache_control))
    ctx.complete(Response(status_code=304, headers=headers))


def _send_range_not_satisfiable(
    ctx: HTTPReqCtx,
    method: bytes,
    size: int,
    etag: bytes,
    last_modified: bytes,
) -> None:
    ctx.complete(
        Response(
            status_code=416,
            headers=[
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(_RANGE_NOT_SATISFIABLE_BODY)).encode("ascii")),
                (b"content-range", f"bytes */{size}".encode("ascii")),
                (b"etag", etag),
                (b"last-modified", last_modified),
            ],
        ),
        None if method == b"HEAD" else _RANGE_NOT_SATISFIABLE_BODY,
    )


