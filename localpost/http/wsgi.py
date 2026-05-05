from __future__ import annotations

import sys
from collections.abc import Buffer, Callable
from io import RawIOBase
from typing import Any, final, override
from urllib.parse import unquote_to_bytes
from wsgiref.types import WSGIApplication

from localpost.http._base import BodyHandler, HTTPReqCtx, RequestHandler
from localpost.http._types import Response as _Response

__all__ = ["wrap_wsgi"]


@final
class _RequestBodyStream(RawIOBase):
    """Expose the buffered ``HTTPReqCtx.body`` as ``wsgi.input``.

    The body has already been read by the selector before the WSGI app
    runs, so this is a thin slicing view — no I/O, no ``recv``.
    """

    def __init__(self, body: bytes) -> None:
        self._buf = body
        self._pos = 0

    @override
    def writable(self) -> bool:
        return False

    @override
    def seekable(self) -> bool:
        return False

    @override
    def readable(self) -> bool:
        return True

    @override
    def readall(self) -> bytes:
        result = self._buf[self._pos :]
        self._pos = len(self._buf)
        return result

    @override
    def readinto(self, buffer: Buffer, /) -> int:
        view = memoryview(buffer).cast("B")
        n = len(view)
        avail = len(self._buf) - self._pos
        if avail == 0:
            return 0
        take = min(n, avail)
        view[:take] = self._buf[self._pos : self._pos + take]
        self._pos += take
        return take


def wrap_wsgi(app: WSGIApplication) -> RequestHandler:
    """Wrap a WSGI application as a native :class:`RequestHandler`.

    WSGI apps may consume ``wsgi.input`` from any route, so the wrapper
    always returns a :data:`BodyHandler` continuation — the selector
    buffers the request body into ``ctx.body`` before the WSGI app runs.
    """

    def run_wsgi(ctx: HTTPReqCtx) -> None:
        environ = _build_environ(ctx)

        response_state: dict[str, Any] = {}

        def start_response(
            status: str,
            headers: list[tuple[str, str]],
            exc_info: Any = None,
        ) -> Callable[[bytes], None]:
            if exc_info:
                try:
                    if response_state.get("started"):
                        raise exc_info[1].with_traceback(exc_info[2])
                finally:
                    exc_info = None
            status_code = int(status.split(" ", 1)[0])
            reason = status.split(" ", 1)[1] if " " in status else ""
            wire_headers = [(name.encode("iso-8859-1"), value.encode("iso-8859-1")) for name, value in headers]
            response_state["response"] = _Response(
                status_code=status_code,
                headers=wire_headers,
                reason=reason.encode("iso-8859-1") if reason else b"",
            )
            return _wsgi_write_deprecated

        body_iter = app(environ, start_response)
        try:
            first_nonempty: bytes | None = None
            # Materialize the first chunk (even if empty) — guarantees start_response has been called.
            iterator = iter(body_iter)
            for chunk in iterator:
                if chunk:
                    first_nonempty = chunk
                    break
            response = response_state.get("response")
            if response is None:
                raise RuntimeError("WSGI app returned without calling start_response")
            ctx.start_response(response)
            response_state["started"] = True
            if first_nonempty is not None:
                ctx.send(first_nonempty)
                for chunk in iterator:
                    if chunk:
                        ctx.send(chunk)
            ctx.finish_response()
        finally:
            close = getattr(body_iter, "close", None)
            if close is not None:
                close()

    def pre_body(_ctx: HTTPReqCtx) -> BodyHandler:
        return run_wsgi

    return pre_body


def _wsgi_write_deprecated(_: bytes) -> None:
    raise NotImplementedError("The WSGI write() callable is deprecated and not supported")


def _build_environ(ctx: HTTPReqCtx) -> dict[str, Any]:
    request = ctx.request
    # ``request.path`` / ``request.query_string`` are pre-split by the
    # backend; only the percent-decode + ISO-8859-1 decode happens here.
    path = unquote_to_bytes(request.path).decode("iso-8859-1")
    query_string = request.query_string.decode("iso-8859-1")

    server_host, _, server_port = ctx.local_addr.rpartition(":")
    if not server_host:
        server_host, server_port = ctx.local_addr, ""

    environ: dict[str, Any] = {
        "REQUEST_METHOD": request.method.decode("ascii"),
        "SCRIPT_NAME": "",
        "PATH_INFO": path,
        "QUERY_STRING": query_string,
        "CONTENT_TYPE": "",
        "CONTENT_LENGTH": "",
        "SERVER_NAME": server_host,
        "SERVER_PORT": server_port,
        "SERVER_PROTOCOL": f"HTTP/{request.http_version.decode('ascii')}",
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": ctx.scheme,
        "wsgi.input": _RequestBodyStream(ctx.body),
        "wsgi.errors": sys.stderr,
        "wsgi.multithread": True,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }
    if ctx.remote_addr is not None:
        client_host, _, client_port = ctx.remote_addr.rpartition(":")
        if client_host:
            environ["REMOTE_ADDR"] = client_host
            environ["REMOTE_PORT"] = client_port
        else:
            environ["REMOTE_ADDR"] = ctx.remote_addr

    # Single header pass: h11 normalizes names to lowercase bytes, so we can
    # work with bytes directly and decode each name/value exactly once.
    for name, value in request.headers:
        value_str = value.decode("iso-8859-1") if isinstance(value, bytes) else bytes(value).decode("iso-8859-1")
        if name == b"content-type":
            environ["CONTENT_TYPE"] = value_str
        elif name == b"content-length":
            environ["CONTENT_LENGTH"] = value_str
        else:
            # bytes-level upper + replace, then single decode (ASCII for HTTP_* keys).
            key = b"HTTP_" + name.upper().replace(b"-", b"_")
            environ[key.decode("ascii")] = value_str

    return environ
