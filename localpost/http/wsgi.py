from __future__ import annotations

import sys
from collections.abc import Buffer, Callable
from io import RawIOBase
from typing import Any, final, override
from wsgiref.types import WSGIApplication

from localpost.http._types import Response as _NativeResponse
from localpost.http.server import HTTPReqCtx, RequestHandler

__all__ = ["wrap_wsgi"]


@final
class _RequestBodyStream(RawIOBase):
    """Expose ``HTTPReqCtx.receive`` as a readable file-like object for ``wsgi.input``."""

    def __init__(self, ctx: HTTPReqCtx) -> None:
        self._ctx = ctx
        self._eof = False
        self._leftover = b""

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
        buf = bytearray(self._leftover)
        self._leftover = b""
        while not self._eof:
            chunk = self._ctx.receive()
            if not chunk:
                self._eof = True
                break
            buf.extend(chunk)
        return bytes(buf)

    @override
    def readinto(self, buffer: Buffer, /) -> int:
        view = memoryview(buffer).cast("B")
        n = len(view)
        if self._leftover:
            take = min(n, len(self._leftover))
            view[:take] = self._leftover[:take]
            self._leftover = self._leftover[take:]
            return take
        if self._eof:
            return 0
        data = self._ctx.receive(n)
        if not data:
            self._eof = True
            return 0
        if len(data) <= n:
            view[: len(data)] = data
            return len(data)
        view[:n] = data[:n]
        self._leftover = data[n:]
        return n


def wrap_wsgi(app: WSGIApplication) -> RequestHandler:
    """Wrap a WSGI application as a native :class:`RequestHandler`."""

    def handler(ctx: HTTPReqCtx) -> None:
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
            wire_headers = [
                (name.encode("iso-8859-1"), value.encode("iso-8859-1")) for name, value in headers
            ]
            response_state["response"] = _NativeResponse(
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

    return handler


def _wsgi_write_deprecated(_: bytes) -> None:
    raise NotImplementedError("The WSGI write() callable is deprecated and not supported")


def _build_environ(ctx: HTTPReqCtx) -> dict[str, Any]:
    request = ctx.request
    target = request.target.decode("iso-8859-1")
    if "?" in target:
        path, query_string = target.split("?", 1)
    else:
        path, query_string = target, ""

    environ: dict[str, Any] = {
        "REQUEST_METHOD": request.method.decode("ascii"),
        "SCRIPT_NAME": "",
        "PATH_INFO": path,
        "QUERY_STRING": query_string,
        "CONTENT_TYPE": "",
        "CONTENT_LENGTH": "",
        "SERVER_NAME": ctx._server.config.host,
        "SERVER_PORT": str(ctx._server.port),
        "SERVER_PROTOCOL": f"HTTP/{request.http_version.decode('ascii')}",
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": "http",
        "wsgi.input": _RequestBodyStream(ctx),
        "wsgi.errors": sys.stderr,
        "wsgi.multithread": True,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }

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
