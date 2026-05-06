from __future__ import annotations

import contextlib
import sys
from collections.abc import Buffer, Callable, Iterable, Iterator
from dataclasses import dataclass, field
from http import HTTPStatus
from io import RawIOBase
from typing import Any, BinaryIO, final, override
from urllib.parse import unquote_to_bytes
from wsgiref.types import WSGIApplication

from localpost.http._base import BodyHandler, HTTPReqCtx, RequestHandler
from localpost.http._types import InformationalResponse, Request
from localpost.http._types import Response as _Response
from localpost.http.config import DEFAULT_BUFFER_SIZE

__all__ = ["to_wsgi", "wrap_wsgi"]


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


def _wsgi_write_unsupported(_: bytes) -> None:
    raise NotImplementedError("The WSGI write() callable is deprecated and not supported")


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
            return _wsgi_write_unsupported

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


# ---------------------------------------------------------------------------
# Reverse direction: serve a localpost RequestHandler under a WSGI server.
# ---------------------------------------------------------------------------


@final
@dataclass(slots=True, eq=False)
class _WSGIReqCtx:
    """:class:`HTTPReqCtx` implementation backed by a WSGI ``environ``.

    Body is read from ``wsgi.input`` and pre-buffered into ``ctx.body``
    before the handler runs (matches the JSON-API common case the native
    server is tuned for). ``receive`` slices the buffered body for any
    handler that wants chunked reads.

    Three response shapes are supported:

    - **Eager** (``complete``): captured into ``_completed`` and returned
      as a single-element iterable.
    - **Declarative streaming** (``stream``): the chunk iterator is
      handed to the WSGI server lazily — no thread / queue, the WSGI
      worker drives iteration and flushing.
    - **Imperative** (``start_response`` + ``send`` + ``finish_response``):
      chunks are appended to a list. The handler runs synchronously, so
      the full list is returned at the end. **No per-chunk pacing** on
      this path — for true streaming use :meth:`stream`.

    ``borrow()`` is a no-op CM (the WSGI worker already owns the
    connection); ``borrowed`` is always ``True``.
    """

    request: Request
    body: bytes
    remote_addr: str | None
    local_addr: str
    scheme: str
    _environ: dict[str, Any]
    response_status: int | None = None
    attrs: dict[Any, Any] = field(default_factory=dict)
    _body_cursor: int = 0

    # Response capture state — at most one of these is populated.
    _completed: tuple[_Response, bytes] | None = None
    _streaming: tuple[_Response, Iterator[bytes]] | None = None
    _imperative_response: _Response | None = None
    _imperative_chunks: list[bytes] = field(default_factory=list)
    _sendfile: tuple[_Response, BinaryIO, int, int] | None = None

    @property
    def disconnected(self) -> bool:
        # WSGI exposes no socket handle; host-server disconnects surface as
        # ``BrokenPipeError`` from the per-chunk write path during streaming.
        return False

    @property
    def borrowed(self) -> bool:
        return True

    def borrow(self) -> contextlib.AbstractContextManager[HTTPReqCtx]:
        # The WSGI worker already owns the connection — borrow is degenerate.
        return contextlib.nullcontext(self)

    def receive(self, size: int = DEFAULT_BUFFER_SIZE, /) -> bytes:
        if self._body_cursor >= len(self.body):
            return b""
        end = self._body_cursor + size
        chunk = self.body[self._body_cursor : end]
        self._body_cursor += len(chunk)
        return chunk

    # ----- response capture -----

    def complete(self, response: _Response, body: bytes | None = None) -> None:
        self._check_not_started()
        self.response_status = response.status_code
        self._completed = (response, body or b"")

    def start_response(self, response: _Response | InformationalResponse, /) -> None:
        if isinstance(response, InformationalResponse):
            # WSGI's start_response is final-response only; 1xx isn't surfaced.
            return
        self._check_not_started()
        self.response_status = response.status_code
        self._imperative_response = response

    def send(self, chunk: Buffer, /) -> None:
        if self._imperative_response is None:
            raise RuntimeError("send() before start_response()")
        self._imperative_chunks.append(bytes(chunk))

    def finish_response(self) -> None:
        if self._imperative_response is None:
            raise RuntimeError("finish_response() before start_response()")

    def stream(self, response: _Response, chunks: Iterator[bytes], /) -> None:
        self._check_not_started()
        self.response_status = response.status_code
        self._streaming = (response, chunks)

    def sendfile(self, response: _Response, file: BinaryIO, offset: int, count: int) -> None:
        self._check_not_started()
        self.response_status = response.status_code
        self._sendfile = (response, file, offset, count)

    # ----- WSGI-side dispatch -----

    def _respond(self, start_response: Callable[..., Any]) -> Iterable[bytes]:
        if self._completed is not None:
            response, body = self._completed
            start_response(_status_line(response), _wsgi_headers(response.headers))
            return [body] if body else []
        if self._streaming is not None:
            response, chunks = self._streaming
            start_response(_status_line(response), _wsgi_headers(response.headers))
            return chunks
        if self._sendfile is not None:
            response, file, offset, count = self._sendfile
            start_response(_status_line(response), _wsgi_headers(response.headers))
            file.seek(offset)
            file_wrapper = self._environ.get("wsgi.file_wrapper")
            if file_wrapper is not None:
                return file_wrapper(_LimitedReader(file, count), DEFAULT_BUFFER_SIZE)
            return _read_chunks(file, count, DEFAULT_BUFFER_SIZE)
        if self._imperative_response is not None:
            response = self._imperative_response
            start_response(_status_line(response), _wsgi_headers(response.headers))
            return self._imperative_chunks
        raise RuntimeError("Handler returned without producing a response")

    def _check_not_started(self) -> None:
        if (
            self._completed is not None
            or self._streaming is not None
            or self._sendfile is not None
            or self._imperative_response is not None
        ):
            raise RuntimeError("Response already started")


def to_wsgi(handler: RequestHandler) -> WSGIApplication:
    """Serve a localpost :data:`RequestHandler` under a WSGI server.

    The returned WSGI app builds a :class:`HTTPReqCtx` from the WSGI
    ``environ``, drives the handler (pre-body and body-handler phases
    both run synchronously inside the WSGI app call), and translates the
    ctx's captured response into the WSGI return shape:

    - ``ctx.complete(...)`` → ``[body]`` iterable, single ``start_response``.
    - ``ctx.stream(...)`` → the chunk iterator handed straight to the WSGI
      server (lazy, true per-chunk flushing).
    - ``ctx.sendfile(...)`` → ``wsgi.file_wrapper`` if the host server
      provides it, else a chunked read+yield fallback.
    - imperative ``start_response`` / ``send`` / ``finish_response`` →
      chunks buffered in a list and returned as the WSGI body iterable.
      No per-chunk pacing on this path; if you need true streaming,
      use :meth:`HTTPReqCtx.stream`.

    Deployment::

        # myapp.py
        from localpost.http.wsgi import to_wsgi
        from localpost.openapi import HttpApp

        app = HttpApp()


        @app.get("/hello/{name}")
        def hello(name: str) -> str:
            return f"Hello, {name}!"


        wsgi_app = to_wsgi(app._build_router_handler())  # or app.as_wsgi()

    Then ``gunicorn myapp:wsgi_app``.

    :func:`localpost.http.thread_pool_handler` does not apply — the WSGI
    server's worker model is the pool. :func:`check_cancelled` is a
    no-op (the WSGI app has no socket handle); SSE streams over
    :meth:`HTTPReqCtx.stream` surface client disconnects as
    :class:`BrokenPipeError` from the host's per-chunk write.
    """

    def wsgi_app(environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        ctx = _build_wsgi_ctx(environ)
        body_handler = handler(ctx)
        if body_handler is not None:
            body_handler(ctx)
        return ctx._respond(start_response)

    return wsgi_app


def _build_wsgi_ctx(environ: dict[str, Any]) -> _WSGIReqCtx:
    method = environ.get("REQUEST_METHOD", "GET").encode("ascii")
    path_info = environ.get("PATH_INFO", "").encode("iso-8859-1")
    query_string = environ.get("QUERY_STRING", "").encode("iso-8859-1")
    target = path_info + (b"?" + query_string if query_string else b"")

    headers: list[tuple[bytes, bytes]] = []
    if environ.get("CONTENT_TYPE"):
        headers.append((b"content-type", str(environ["CONTENT_TYPE"]).encode("iso-8859-1")))
    if environ.get("CONTENT_LENGTH"):
        headers.append((b"content-length", str(environ["CONTENT_LENGTH"]).encode("iso-8859-1")))
    for key, value in environ.items():
        if not key.startswith("HTTP_"):
            continue
        # HTTP_X_FOO -> x-foo
        name = key[5:].replace("_", "-").lower().encode("ascii")
        headers.append((name, str(value).encode("iso-8859-1")))

    proto = environ.get("SERVER_PROTOCOL", "HTTP/1.1")
    http_version = proto.split("/", 1)[1].encode("ascii") if "/" in proto else b"1.1"

    request = Request(
        method=method,
        target=target,
        path=path_info,
        query_string=query_string,
        headers=tuple(headers),
        http_version=http_version,
    )

    body = _read_body(environ)

    server_host = str(environ.get("SERVER_NAME", ""))
    server_port = str(environ.get("SERVER_PORT", ""))
    local_addr = f"{server_host}:{server_port}" if server_port else server_host

    remote_host = environ.get("REMOTE_ADDR")
    if remote_host:
        remote_port = environ.get("REMOTE_PORT")
        remote_addr: str | None = f"{remote_host}:{remote_port}" if remote_port else str(remote_host)
    else:
        remote_addr = None

    return _WSGIReqCtx(
        request=request,
        body=body,
        remote_addr=remote_addr,
        local_addr=local_addr,
        scheme=str(environ.get("wsgi.url_scheme", "http")),
        _environ=environ,
    )


def _read_body(environ: dict[str, Any]) -> bytes:
    cl_raw = environ.get("CONTENT_LENGTH") or ""
    try:
        cl = int(cl_raw) if cl_raw else 0
    except ValueError:
        cl = 0
    if cl <= 0:
        return b""
    stream = environ.get("wsgi.input")
    if stream is None:
        return b""
    return stream.read(cl)


def _status_line(response: _Response) -> str:
    if response.reason:
        reason = response.reason.decode("iso-8859-1")
    else:
        try:
            reason = HTTPStatus(response.status_code).phrase
        except ValueError:
            reason = ""
    return f"{response.status_code} {reason}"


def _wsgi_headers(headers: Any) -> list[tuple[str, str]]:
    return [(name.decode("iso-8859-1"), value.decode("iso-8859-1")) for name, value in headers]


@final
class _LimitedReader:
    """``file_wrapper``-friendly view of a file restricted to ``count`` bytes
    starting from the current position. Servers that consume
    ``wsgi.file_wrapper`` either ``sendfile()`` the underlying fd or fall
    back to ``.read(blksize)`` — both paths are honoured here.
    """

    __slots__ = ("_file", "_remaining")

    def __init__(self, file: BinaryIO, count: int) -> None:
        self._file = file
        self._remaining = count

    def read(self, n: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        if n < 0 or n > self._remaining:
            n = self._remaining
        data = self._file.read(n)
        self._remaining -= len(data)
        return data

    def fileno(self) -> int:
        return self._file.fileno()


def _read_chunks(file: BinaryIO, count: int, blksize: int) -> Iterator[bytes]:
    """Fallback for hosts without ``wsgi.file_wrapper``."""
    remaining = count
    while remaining > 0:
        chunk = file.read(min(blksize, remaining))
        if not chunk:
            return
        remaining -= len(chunk)
        yield chunk
