"""Response compression middleware (dynamic responses only).

Wraps a :data:`RequestHandler` so that responses emitted via
:meth:`HTTPReqCtx.complete` are compressed when the client accepts it,
the response is large enough to be worth compressing, and the
content type is in the allowlist.

Streaming response paths (``start_response`` / ``send`` /
``finish_response``) and zero-copy ``sendfile`` pass through
**uncompressed** in v1 — composition with the static handler stays
zero-copy. See ``plans/compression-middleware.md`` for the rationale
and follow-ups.

Encodings:

- ``gzip`` — stdlib, always available
- ``br`` — optional via the ``[http-compress]`` extra (``brotli``)

If ``"br"`` appears in ``algorithms`` but :mod:`brotli` isn't
installed, :func:`compress_handler` raises :class:`ImportError` at
construction time — fail fast rather than mid-request.
"""

from __future__ import annotations

import functools
import gzip
import importlib
import importlib.util
import zlib
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, BinaryIO, Final

from localpost.http._base import BodyHandler, HTTPReqCtx, RequestHandler, native_stream
from localpost.http._types import InformationalResponse, Request, Response

if TYPE_CHECKING:
    from collections.abc import Buffer

__all__ = [
    "DEFAULT_COMPRESSIBLE_TYPES",
    "compress_handler",
]


DEFAULT_COMPRESSIBLE_TYPES: Final[frozenset[bytes]] = frozenset(
    {
        b"text/html",
        b"text/plain",
        b"text/css",
        b"text/xml",
        b"text/csv",
        b"text/javascript",
        b"text/event-stream",
        b"application/json",
        b"application/javascript",
        b"application/xml",
        b"application/xhtml+xml",
        b"application/manifest+json",
        b"application/x-yaml",
        b"application/rss+xml",
        b"application/atom+xml",
        b"image/svg+xml",
    }
)


# --------------------------------------------------------------------------
# Encoder registry
# --------------------------------------------------------------------------


def _compress_gzip(data: bytes) -> bytes:
    return gzip.compress(data, compresslevel=6)


# Imported lazily — the dependency is in the optional ``[http-compress]``
# extra. ``importlib`` keeps the static import out of ty/pyright's sight,
# so missing ``brotli`` doesn't fail type-checking.
@functools.cache
def _brotli() -> Any:
    return importlib.import_module("brotli")


def _compress_brotli(data: bytes) -> bytes:
    # quality=4 favours speed over ratio (brotli default 11 is very slow).
    return _brotli().compress(data, quality=4)


_ENCODERS: dict[str, Callable[[bytes], bytes]] = {
    "gzip": _compress_gzip,
    "br": _compress_brotli,
}


# --------------------------------------------------------------------------
# Streaming encoders (used by the streaming-response path)
# --------------------------------------------------------------------------


class _StreamEncoder:
    """Per-request streaming compressor.

    ``compress(data)`` is incremental — output may be empty until the
    encoder has buffered enough. ``flush()`` emits a sync-flush block so
    the decompressor can produce the bytes seen so far (the SSE
    invariant: each event reaches the client promptly). ``finish()`` is
    the terminal flush.
    """

    def compress(self, data: bytes) -> bytes:  # pragma: no cover — abstract
        raise NotImplementedError

    def flush(self) -> bytes:  # pragma: no cover — abstract
        raise NotImplementedError

    def finish(self) -> bytes:  # pragma: no cover — abstract
        raise NotImplementedError


class _GzipStreamEncoder(_StreamEncoder):
    """``zlib.compressobj(wbits=31)`` produces gzip-format output (header +
    deflate + trailer). ``Z_SYNC_FLUSH`` between events; ``Z_FINISH`` at end.
    """

    def __init__(self) -> None:
        self._cobj = zlib.compressobj(level=6, method=zlib.DEFLATED, wbits=31)

    def compress(self, data: bytes) -> bytes:
        return self._cobj.compress(data)

    def flush(self) -> bytes:
        return self._cobj.flush(zlib.Z_SYNC_FLUSH)

    def finish(self) -> bytes:
        return self._cobj.flush(zlib.Z_FINISH)


class _BrotliStreamEncoder(_StreamEncoder):
    """``brotli.Compressor.flush()`` emits a flush-block (sync-flush
    semantics); ``finish()`` ends the stream.
    """

    def __init__(self) -> None:
        self._c = _brotli().Compressor(quality=4)

    def compress(self, data: bytes) -> bytes:
        return self._c.process(data)

    def flush(self) -> bytes:
        return self._c.flush()

    def finish(self) -> bytes:
        return self._c.finish()


_STREAM_ENCODER_FACTORIES: dict[str, Callable[[], _StreamEncoder]] = {
    "gzip": _GzipStreamEncoder,
    "br": _BrotliStreamEncoder,
}


def _check_algorithm_available(name: str) -> None:
    if name == "br":
        if importlib.util.find_spec("brotli") is None:
            raise ImportError(
                "compress_handler(algorithms=...) includes 'br' but the optional "
                "[http-compress] extra (brotli) is not installed."
            )
    elif name == "gzip":
        return  # stdlib
    else:
        raise ValueError(f"Unsupported compression algorithm: {name!r}")


# --------------------------------------------------------------------------
# Negotiation
# --------------------------------------------------------------------------


def _negotiate(accept_encoding: bytes, server_pref: Sequence[str]) -> str | None:
    """Pick the best mutually-supported encoding.

    Parses ``Accept-Encoding`` per RFC 7231 §5.3.4: comma-separated
    tokens, optional ``;q=<float>``, default ``q=1.0``. ``q=0`` disables.
    ``*`` is a wildcard. Returns the highest-server-preference encoding
    with q>0, or ``None`` if nothing matches.
    """
    qs: dict[str, float] = {}
    star_q: float | None = None
    for raw in accept_encoding.split(b","):
        token = raw.strip()
        if not token:
            continue
        name_b, sep, params = token.partition(b";")
        name = name_b.strip().decode("ascii", errors="replace").lower()
        q = 1.0
        if sep:
            for param in params.split(b";"):
                k, kv_sep, v = param.strip().partition(b"=")
                if kv_sep and k.strip().lower() == b"q":
                    try:
                        q = float(v.strip())
                    except ValueError:
                        q = 0.0
                    break
        if name == "*":
            star_q = q
        else:
            qs[name] = q
    for pref in server_pref:
        q = qs.get(pref)
        if q is None and star_q is not None:
            q = star_q
        if q is not None and q > 0:
            return pref
    return None


# --------------------------------------------------------------------------
# Header inspection / rewrite
# --------------------------------------------------------------------------


def _get_header(headers: Sequence[tuple[bytes, bytes]], name: bytes) -> bytes | None:
    name_lc = name.lower()
    for n, v in headers:
        if n.lower() == name_lc:
            return v
    return None


def _is_compressible_response(
    response: Response,
    body: bytes | None,
    *,
    method: bytes,
    min_size: int,
    compressible_types: frozenset[bytes],
) -> bool:
    """Apply the v1 decision matrix from the design doc."""
    if method == b"HEAD":
        return False
    if body is None or len(body) < min_size:
        return False
    status = response.status_code
    if status in {204, 304} or 100 <= status < 200 or status == 206:
        return False
    # Header-driven exclusions.
    has_compressible_type = False
    for name, value in response.headers:
        n = name.lower()
        if n == b"content-encoding":
            v = value.strip().lower()
            if v and v != b"identity":
                return False
        elif n == b"cache-control":
            # Conservative substring check — ``no-transform`` is a directive
            # token, not a value, so substring matches without false positives.
            if b"no-transform" in value.lower():
                return False
        elif n == b"content-type" and not has_compressible_type:
            main_type = value.split(b";", 1)[0].strip().lower()
            if main_type in compressible_types:
                has_compressible_type = True
    return has_compressible_type


def _rewrite_headers(
    headers: Sequence[tuple[bytes, bytes]],
    *,
    encoding: str,
    new_length: int,
) -> list[tuple[bytes, bytes]]:
    """Replace ``Content-Length``, add ``Content-Encoding``, merge
    ``Vary: Accept-Encoding``.
    """
    enc_value = encoding.encode("ascii")
    out: list[tuple[bytes, bytes]] = []
    vary_seen = False
    cl_replaced = False
    for name, value in headers:
        n = name.lower()
        if n == b"content-length":
            if not cl_replaced:
                out.append((name, str(new_length).encode("ascii")))
                cl_replaced = True
            # Drop duplicate Content-Length headers if any.
        elif n == b"vary":
            vary_seen = True
            out.append((name, _merge_vary(value)))
        else:
            out.append((name, value))
    if not cl_replaced:
        out.append((b"content-length", str(new_length).encode("ascii")))
    if not vary_seen:
        out.append((b"vary", b"Accept-Encoding"))
    out.append((b"content-encoding", enc_value))
    return out


def _is_streaming_eligible(
    response: Response,
    *,
    method: bytes,
    compressible_types: frozenset[bytes],
) -> bool:
    """Decision matrix for the streaming-response path.

    No body-size check (we don't know it). Otherwise mirrors
    :func:`_is_compressible_response` plus an additional rule: skip when
    the handler declared a ``Content-Length`` (a known-length response
    is contractually fixed; compressing mid-stream would lie about the
    declared length).
    """
    if method == b"HEAD":
        return False
    status = response.status_code
    if status in {204, 304} or 100 <= status < 200 or status == 206:
        return False
    has_compressible_type = False
    for name, value in response.headers:
        n = name.lower()
        if n == b"content-length":
            return False  # known-length stream — pass through verbatim
        if n == b"content-encoding":
            v = value.strip().lower()
            if v and v != b"identity":
                return False
        elif n == b"cache-control":
            if b"no-transform" in value.lower():
                return False
        elif n == b"content-type" and not has_compressible_type:
            main_type = value.split(b";", 1)[0].strip().lower()
            if main_type in compressible_types:
                has_compressible_type = True
    return has_compressible_type


def _streaming_rewrite_headers(
    headers: Sequence[tuple[bytes, bytes]],
    *,
    encoding: str,
) -> list[tuple[bytes, bytes]]:
    """Add ``Content-Encoding``, merge ``Vary``. ``Content-Length`` is
    dropped (eligibility already rejected the case where it was set).
    ``Transfer-Encoding`` is left to the backend — both auto-add
    ``chunked`` for HTTP/1.1 peers when no framing is present.
    """
    enc_value = encoding.encode("ascii")
    out: list[tuple[bytes, bytes]] = []
    vary_seen = False
    for name, value in headers:
        n = name.lower()
        if n == b"content-length":
            continue  # defensive — eligibility check rejects this case
        if n == b"vary":
            vary_seen = True
            out.append((name, _merge_vary(value)))
        else:
            out.append((name, value))
    if not vary_seen:
        out.append((b"vary", b"Accept-Encoding"))
    out.append((b"content-encoding", enc_value))
    return out


def _merge_vary(existing: bytes) -> bytes:
    """Merge ``Accept-Encoding`` into an existing ``Vary`` header value."""
    stripped = existing.strip()
    if stripped == b"*":
        return existing
    tokens = [t.strip() for t in stripped.split(b",") if t.strip()]
    for t in tokens:
        if t.lower() == b"accept-encoding":
            return existing
    tokens.append(b"Accept-Encoding")
    return b", ".join(tokens)


# --------------------------------------------------------------------------
# Proxy ctx
# --------------------------------------------------------------------------


@dataclass(slots=True, eq=False)
class _CompressedCtx:
    """Forwarding proxy that intercepts :meth:`complete` to compress.

    All other methods / attributes pass through to ``_inner``. The
    proxy is allocated once per request — only when the request is
    actually eligible for compression — so the overhead is bounded.
    """

    _inner: HTTPReqCtx
    _encoding: str
    _min_size: int
    _compressible_types: frozenset[bytes]
    # Streaming-mode state. ``_stream_encoder`` is non-None between a
    # final ``start_response`` (eligible) and ``finish_response``.
    _stream_encoder: _StreamEncoder | None = field(default=None, init=False)

    # ----- forwarded attributes -----

    @property
    def request(self) -> Request:
        return self._inner.request

    @property
    def body(self) -> bytes:
        return self._inner.body

    @property
    def attrs(self) -> dict[Any, Any]:
        return self._inner.attrs

    @property
    def response_status(self) -> int | None:
        return self._inner.response_status

    @property
    def remote_addr(self) -> str | None:
        return self._inner.remote_addr

    @property
    def local_addr(self) -> str:
        return self._inner.local_addr

    @property
    def scheme(self) -> str:
        return self._inner.scheme

    @property
    def borrowed(self) -> bool:
        return self._inner.borrowed

    def borrow(self) -> AbstractContextManager[HTTPReqCtx]:
        return self._inner.borrow()

    def receive(self, size: int = 65536, /) -> bytes:
        return self._inner.receive(size)

    # ----- intercepted streaming-response API -----

    def start_response(self, response: Response | InformationalResponse, /) -> None:
        # 1xx responses pass through verbatim (multiple may precede the final).
        if isinstance(response, InformationalResponse):
            self._inner.start_response(response)
            return
        # Final Response. Decide streaming compression eligibility.
        if _is_streaming_eligible(
            response,
            method=self._inner.request.method,
            compressible_types=self._compressible_types,
        ):
            self._stream_encoder = _STREAM_ENCODER_FACTORIES[self._encoding]()
            new_headers = _streaming_rewrite_headers(response.headers, encoding=self._encoding)
            self._inner.start_response(
                Response(status_code=response.status_code, headers=new_headers, reason=response.reason),
            )
        else:
            self._inner.start_response(response)

    def send(self, chunk: Buffer, /) -> None:
        if self._stream_encoder is None:
            self._inner.send(chunk)
            return
        chunk_bytes = chunk if isinstance(chunk, bytes) else bytes(chunk)
        if not chunk_bytes:
            # Empty send (sometimes used to flush headers): pass through
            # so we don't emit a sync-marker for nothing.
            self._inner.send(chunk_bytes)
            return
        out = self._stream_encoder.compress(chunk_bytes) + self._stream_encoder.flush()
        if out:
            self._inner.send(out)

    def finish_response(self) -> None:
        if self._stream_encoder is not None:
            tail = self._stream_encoder.finish()
            self._stream_encoder = None
            if tail:
                self._inner.send(tail)
        self._inner.finish_response()

    def sendfile(self, response: Response, file: BinaryIO, offset: int, count: int) -> None:
        # Pass-through: zero-copy is preserved (compression and sendfile
        # are at odds; see plans/compression-middleware.md).
        self._inner.sendfile(response, file, offset, count)

    def stream(self, response: Response, chunks: Iterator[bytes], /) -> None:
        # Default impl: drives the imperative trio on ``self``, which the
        # compression middleware already intercepts per-chunk.
        native_stream(self, response, chunks)

    # ----- intercepted -----

    def complete(self, response: Response, body: bytes | None = None) -> None:
        if not _is_compressible_response(
            response,
            body,
            method=self._inner.request.method,
            min_size=self._min_size,
            compressible_types=self._compressible_types,
        ):
            self._inner.complete(response, body)
            return
        assert body is not None  # _is_compressible_response gates on body is not None
        compressed = _ENCODERS[self._encoding](body)
        new_headers = _rewrite_headers(
            response.headers,
            encoding=self._encoding,
            new_length=len(compressed),
        )
        self._inner.complete(
            Response(status_code=response.status_code, headers=new_headers, reason=response.reason),
            compressed,
        )


# --------------------------------------------------------------------------
# Public entry
# --------------------------------------------------------------------------


def compress_handler(
    inner: RequestHandler,
    *,
    algorithms: Sequence[str] = ("br", "gzip"),
    min_size: int = 1024,
    compressible_types: frozenset[bytes] = DEFAULT_COMPRESSIBLE_TYPES,
) -> RequestHandler:
    """Wrap ``inner`` so eligible responses are compressed.

    Args:
        inner: The wrapped :data:`RequestHandler`.
        algorithms: Server preference order. Each entry must be one of
            ``"gzip"`` (stdlib) or ``"br"`` (requires the
            ``[http-compress]`` extra). The first algorithm that the
            client accepts (per ``Accept-Encoding`` q-values) is chosen.
        min_size: Minimum response body size to trigger compression. Below
            this, the framing / re-allocation overhead dominates the
            compression gain. Default ``1024``.
        compressible_types: Allowlist of lowercased ``Content-Type``
            main-types (split on ``;``). See
            :data:`DEFAULT_COMPRESSIBLE_TYPES` for the default.

    Raises:
        ValueError: ``algorithms`` is empty or contains an unknown name.
        ImportError: ``"br"`` is requested but :mod:`brotli` is not
            installed (install via ``pip install localpost[http-compress]``).

    Returns:
        A :data:`RequestHandler` that wraps ``inner``. Compression only
        intercepts :meth:`HTTPReqCtx.complete`; streaming responses
        (``start_response`` / ``send`` / ``finish_response``) and
        :meth:`HTTPReqCtx.sendfile` pass through unchanged.
    """
    if not algorithms:
        raise ValueError("compress_handler: ``algorithms`` must be non-empty")
    for name in algorithms:
        _check_algorithm_available(name)
    if min_size < 0:
        raise ValueError("compress_handler: ``min_size`` must be >= 0")

    server_pref = tuple(algorithms)

    def wrapped(ctx: HTTPReqCtx) -> BodyHandler | None:
        accept = _get_header(ctx.request.headers, b"accept-encoding")
        if accept is None or ctx.request.method == b"HEAD":
            return inner(ctx)
        chosen = _negotiate(accept, server_pref)
        if chosen is None:
            return inner(ctx)
        proxy = _CompressedCtx(
            _inner=ctx,
            _encoding=chosen,
            _min_size=min_size,
            _compressible_types=compressible_types,
        )
        result = inner(proxy)  # type: ignore[arg-type]  # _CompressedCtx structurally satisfies HTTPReqCtx
        if result is None:
            return None
        # If the inner returned a BodyHandler continuation, route it
        # through the same proxy so its ``complete`` call lands here too.
        body_handler: BodyHandler = result

        def proxied_body(real_ctx: HTTPReqCtx) -> None:
            body_handler(
                _CompressedCtx(  # type: ignore[arg-type]
                    _inner=real_ctx,
                    _encoding=chosen,
                    _min_size=min_size,
                    _compressible_types=compressible_types,
                )
            )

        return proxied_body

    return wrapped
