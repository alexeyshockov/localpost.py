"""Tests for localpost.http.compress — gzip/brotli response middleware."""

from __future__ import annotations

import gzip
import importlib.util
import zlib
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from localpost.http import (
    HTTPReqCtx,
    Response,
    compress_handler,
    static_handler,
)
from localpost.http.compress import (
    DEFAULT_COMPRESSIBLE_TYPES,
    _is_compressible_response,
    _is_streaming_eligible,
    _merge_vary,
    _negotiate,
    _rewrite_headers,
    _streaming_rewrite_headers,
)

_HAS_BROTLI = importlib.util.find_spec("brotli") is not None


# --- Negotiation ---------------------------------------------------------


class TestNegotiate:
    def test_empty_returns_none(self):
        assert _negotiate(b"", ("br", "gzip")) is None

    def test_simple(self):
        assert _negotiate(b"gzip", ("br", "gzip")) == "gzip"

    def test_preference_order(self):
        # Server prefers br first; client lists both → pick br.
        assert _negotiate(b"gzip, br", ("br", "gzip")) == "br"

    def test_unsupported_returns_none(self):
        assert _negotiate(b"deflate", ("br", "gzip")) is None

    def test_q_zero_disables(self):
        assert _negotiate(b"gzip;q=0, br", ("gzip", "br")) == "br"

    def test_only_q_zero_returns_none(self):
        assert _negotiate(b"gzip;q=0", ("gzip",)) is None

    def test_wildcard(self):
        assert _negotiate(b"*", ("br", "gzip")) == "br"

    def test_wildcard_with_explicit_disable(self):
        # ``*;q=1, gzip;q=0`` → gzip explicitly disabled, br matches via wildcard.
        assert _negotiate(b"gzip;q=0, *;q=1", ("gzip", "br")) == "br"

    def test_q_value_parsing_invalid_treated_as_zero(self):
        assert _negotiate(b"gzip;q=abc", ("gzip",)) is None

    def test_whitespace_tolerant(self):
        assert _negotiate(b"  gzip ;q= 1.0 , br ", ("br", "gzip")) == "br"


# --- _is_compressible_response ------------------------------------------


def _r(status: int = 200, headers=None) -> Response:
    return Response(status_code=status, headers=list(headers or []))


class TestIsCompressible:
    def test_basic_yes(self):
        r = _r(headers=[(b"content-type", b"application/json")])
        assert _is_compressible_response(
            r, b"x" * 2048, method=b"GET", min_size=1024, compressible_types=DEFAULT_COMPRESSIBLE_TYPES
        )

    def test_method_head(self):
        r = _r(headers=[(b"content-type", b"application/json")])
        assert not _is_compressible_response(
            r, b"x" * 2048, method=b"HEAD", min_size=1024, compressible_types=DEFAULT_COMPRESSIBLE_TYPES
        )

    def test_body_none(self):
        r = _r(headers=[(b"content-type", b"application/json")])
        assert not _is_compressible_response(
            r, None, method=b"GET", min_size=1024, compressible_types=DEFAULT_COMPRESSIBLE_TYPES
        )

    def test_below_min_size(self):
        r = _r(headers=[(b"content-type", b"application/json")])
        assert not _is_compressible_response(
            r, b"x" * 100, method=b"GET", min_size=1024, compressible_types=DEFAULT_COMPRESSIBLE_TYPES
        )

    @pytest.mark.parametrize("status", [100, 101, 199, 204, 304, 206])
    def test_no_body_statuses(self, status):
        r = _r(status, headers=[(b"content-type", b"application/json")])
        assert not _is_compressible_response(
            r, b"x" * 2048, method=b"GET", min_size=1024, compressible_types=DEFAULT_COMPRESSIBLE_TYPES
        )

    def test_existing_content_encoding(self):
        r = _r(headers=[(b"content-type", b"application/json"), (b"content-encoding", b"gzip")])
        assert not _is_compressible_response(
            r, b"x" * 2048, method=b"GET", min_size=1024, compressible_types=DEFAULT_COMPRESSIBLE_TYPES
        )

    def test_identity_encoding_treated_as_compressible(self):
        r = _r(headers=[(b"content-type", b"application/json"), (b"content-encoding", b"identity")])
        assert _is_compressible_response(
            r, b"x" * 2048, method=b"GET", min_size=1024, compressible_types=DEFAULT_COMPRESSIBLE_TYPES
        )

    def test_no_transform_directive(self):
        r = _r(headers=[(b"content-type", b"application/json"), (b"cache-control", b"public, no-transform")])
        assert not _is_compressible_response(
            r, b"x" * 2048, method=b"GET", min_size=1024, compressible_types=DEFAULT_COMPRESSIBLE_TYPES
        )

    def test_content_type_with_charset(self):
        # ``application/json; charset=utf-8`` — main type splits cleanly.
        r = _r(headers=[(b"content-type", b"application/json; charset=utf-8")])
        assert _is_compressible_response(
            r, b"x" * 2048, method=b"GET", min_size=1024, compressible_types=DEFAULT_COMPRESSIBLE_TYPES
        )

    def test_non_compressible_type(self):
        r = _r(headers=[(b"content-type", b"image/png")])
        assert not _is_compressible_response(
            r, b"x" * 2048, method=b"GET", min_size=1024, compressible_types=DEFAULT_COMPRESSIBLE_TYPES
        )

    def test_no_content_type(self):
        r = _r(headers=[])
        assert not _is_compressible_response(
            r, b"x" * 2048, method=b"GET", min_size=1024, compressible_types=DEFAULT_COMPRESSIBLE_TYPES
        )

    def test_user_supplied_allowlist(self):
        custom = frozenset({b"application/x-custom"})
        r = _r(headers=[(b"content-type", b"application/x-custom")])
        assert _is_compressible_response(r, b"x" * 2048, method=b"GET", min_size=1024, compressible_types=custom)
        r2 = _r(headers=[(b"content-type", b"application/json")])
        assert not _is_compressible_response(r2, b"x" * 2048, method=b"GET", min_size=1024, compressible_types=custom)


# --- Header rewrite ------------------------------------------------------


class TestRewriteHeaders:
    def test_replaces_content_length(self):
        out = _rewrite_headers(
            [(b"content-type", b"application/json"), (b"content-length", b"100")],
            encoding="gzip",
            new_length=42,
        )
        d = dict(out)
        assert d[b"content-length"] == b"42"

    def test_adds_content_length_if_missing(self):
        out = _rewrite_headers(
            [(b"content-type", b"application/json")],
            encoding="gzip",
            new_length=42,
        )
        d = dict(out)
        assert d[b"content-length"] == b"42"

    def test_adds_content_encoding(self):
        out = _rewrite_headers(
            [(b"content-type", b"application/json"), (b"content-length", b"100")],
            encoding="br",
            new_length=42,
        )
        d = dict(out)
        assert d[b"content-encoding"] == b"br"

    def test_vary_added_if_missing(self):
        out = _rewrite_headers([(b"content-length", b"100")], encoding="gzip", new_length=42)
        d = dict(out)
        assert d[b"vary"] == b"Accept-Encoding"

    def test_vary_merged_with_existing(self):
        out = _rewrite_headers(
            [(b"content-length", b"100"), (b"vary", b"Cookie")],
            encoding="gzip",
            new_length=42,
        )
        d = dict(out)
        assert d[b"vary"] == b"Cookie, Accept-Encoding"

    def test_vary_star_left_alone(self):
        out = _rewrite_headers(
            [(b"content-length", b"100"), (b"vary", b"*")],
            encoding="gzip",
            new_length=42,
        )
        d = dict(out)
        assert d[b"vary"] == b"*"

    def test_vary_no_duplicate_when_already_listed(self):
        out = _rewrite_headers(
            [(b"content-length", b"100"), (b"vary", b"Accept-Encoding, Cookie")],
            encoding="gzip",
            new_length=42,
        )
        d = dict(out)
        assert d[b"vary"] == b"Accept-Encoding, Cookie"


class TestMergeVary:
    def test_empty(self):
        assert _merge_vary(b"") == b"Accept-Encoding"

    def test_single(self):
        assert _merge_vary(b"Cookie") == b"Cookie, Accept-Encoding"

    def test_already_present(self):
        assert _merge_vary(b"Accept-Encoding, Cookie") == b"Accept-Encoding, Cookie"
        assert _merge_vary(b"accept-encoding") == b"accept-encoding"  # case-insensitive

    def test_star(self):
        assert _merge_vary(b"*") == b"*"


# --- Construction --------------------------------------------------------


class TestConstruction:
    def test_empty_algorithms_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            compress_handler(lambda ctx: None, algorithms=())

    def test_unknown_algorithm_rejected(self):
        with pytest.raises(ValueError, match="Unsupported"):
            compress_handler(lambda ctx: None, algorithms=("deflate",))

    def test_negative_min_size_rejected(self):
        with pytest.raises(ValueError, match="min_size"):
            compress_handler(lambda ctx: None, algorithms=("gzip",), min_size=-1)


# --- Integration ---------------------------------------------------------


def _emit(ctx: HTTPReqCtx, content_type: bytes, body: bytes, **headers: bytes) -> None:
    hs: list[tuple[bytes, bytes]] = [
        (b"content-type", content_type),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    for k, v in headers.items():
        hs.append((k.replace("_", "-").encode("ascii"), v))
    # HEAD must not carry a body on the wire; headers describe what GET would send.
    payload: bytes | None = None if ctx.request.method == b"HEAD" else body
    ctx.complete(Response(status_code=200, headers=hs), payload)


def _make_handler(content_type: bytes, body: bytes):
    def handler(ctx: HTTPReqCtx) -> None:
        _emit(ctx, content_type, body)

    return handler


class TestRoundTripGzip:
    def test_compresses_json(self, serve_backend_in_thread):
        body = b'{"data": ' + b"x" * 4096 + b"}"
        h = compress_handler(_make_handler(b"application/json", body), algorithms=("gzip",))
        with serve_backend_in_thread(h) as port:
            # httpx auto-decodes Content-Encoding by default.
            r = httpx.get(
                f"http://127.0.0.1:{port}/",
                headers={"accept-encoding": "gzip"},
                timeout=5,
            )
        assert r.status_code == 200
        assert r.headers["content-encoding"] == "gzip"
        assert r.headers.get("vary", "").lower().find("accept-encoding") >= 0
        # Content-Length describes the compressed body.
        assert int(r.headers["content-length"]) < len(body)
        # httpx decoded it back to the original.
        assert r.content == body

    def test_skips_when_client_doesnt_accept(self, serve_backend_in_thread):
        # ``Accept-Encoding: identity`` is how a client opts out of compression
        # explicitly. (httpx sends a default Accept-Encoding when none is set,
        # so omitting the header doesn't actually mean "no Accept-Encoding".)
        body = b'{"data": ' + b"x" * 4096 + b"}"
        h = compress_handler(_make_handler(b"application/json", body), algorithms=("gzip",))
        with serve_backend_in_thread(h) as port:
            r = httpx.get(
                f"http://127.0.0.1:{port}/",
                headers={"accept-encoding": "identity"},
                timeout=5,
            )
        assert r.status_code == 200
        assert "content-encoding" not in r.headers
        assert r.content == body

    def test_skips_below_min_size(self, serve_backend_in_thread):
        body = b'{"x": 1}'  # tiny
        h = compress_handler(_make_handler(b"application/json", body), algorithms=("gzip",))
        with serve_backend_in_thread(h) as port:
            r = httpx.get(
                f"http://127.0.0.1:{port}/",
                headers={"accept-encoding": "gzip"},
                timeout=5,
            )
        assert "content-encoding" not in r.headers
        assert r.content == body

    def test_skips_non_compressible_type(self, serve_backend_in_thread):
        body = b"\x89PNG\r\n\x1a\n" + b"\x00" * 4096  # pretend PNG payload
        h = compress_handler(_make_handler(b"image/png", body), algorithms=("gzip",))
        with serve_backend_in_thread(h) as port:
            r = httpx.get(
                f"http://127.0.0.1:{port}/",
                headers={"accept-encoding": "gzip"},
                timeout=5,
            )
        assert "content-encoding" not in r.headers
        assert r.content == body

    def test_head_passes_through(self, serve_backend_in_thread):
        body = b"x" * 4096
        h = compress_handler(_make_handler(b"text/plain", body), algorithms=("gzip",))
        with serve_backend_in_thread(h) as port:
            r = httpx.head(
                f"http://127.0.0.1:{port}/",
                headers={"accept-encoding": "gzip"},
                timeout=5,
            )
        # No body for HEAD; we don't pretend-compress to compute length.
        assert "content-encoding" not in r.headers


@pytest.mark.skipif(not _HAS_BROTLI, reason="brotli optional extra not installed")
class TestRoundTripBrotli:
    def test_compresses_with_br(self, serve_backend_in_thread):
        body = b'{"data": ' + b"x" * 4096 + b"}"
        h = compress_handler(_make_handler(b"application/json", body), algorithms=("br", "gzip"))
        with serve_backend_in_thread(h) as port:
            r = httpx.get(
                f"http://127.0.0.1:{port}/",
                headers={"accept-encoding": "br, gzip"},
                timeout=5,
            )
        assert r.status_code == 200
        assert r.headers["content-encoding"] == "br"
        assert r.content == body  # httpx decodes br since brotli is installed


# --- Pass-through composition --------------------------------------------


class TestComposeWithStatic:
    def test_sendfile_passes_through_uncompressed(self, tmp_path: Path, serve_backend_in_thread):
        # The static handler uses ctx.sendfile — compress_handler must not
        # interpose on that path. The downloaded bytes should equal the
        # file bytes verbatim, with no Content-Encoding.
        payload = b"<html>" + b"x" * 4096 + b"</html>"
        (tmp_path / "page.html").write_bytes(payload)
        static = static_handler(tmp_path)
        h = compress_handler(static, algorithms=("gzip",))
        with serve_backend_in_thread(h) as port:
            r = httpx.get(
                f"http://127.0.0.1:{port}/page.html",
                headers={"accept-encoding": "gzip"},
                timeout=5,
            )
        assert r.status_code == 200
        assert "content-encoding" not in r.headers
        assert r.content == payload

    def test_round_trip_when_pool_wraps_compress(self, serve_backend_in_thread):
        # Sanity-check: ``compress_handler`` proxies ``ctx.complete`` through
        # the inner handler; the response body is compressed.
        body = b'{"data": ' + b"x" * 4096 + b"}"

        def inner(ctx: HTTPReqCtx) -> None:
            _emit(ctx, b"application/json", body)

        h = compress_handler(inner, algorithms=("gzip",))
        with serve_backend_in_thread(h) as port:
            r = httpx.post(
                f"http://127.0.0.1:{port}/",
                content=b"",
                headers={"accept-encoding": "gzip", "content-length": "0"},
                timeout=5,
            )
        assert r.headers["content-encoding"] == "gzip"
        assert r.content == body


# --- Manual gzip verification --------------------------------------------


class TestGzipBytes:
    """Decode the response body manually to verify the bytes on the wire are
    actually gzip-framed (httpx auto-decodes, so the round-trip tests above
    can't tell).
    """

    def test_actual_gzip_bytes_on_wire(self, serve_backend_in_thread):
        import socket  # noqa: PLC0415

        body = b"hello world " * 1024
        h = compress_handler(_make_handler(b"text/plain", body), algorithms=("gzip",))
        with serve_backend_in_thread(h) as port, socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nAccept-Encoding: gzip\r\nConnection: close\r\n\r\n")
            buf = b""
            while True:
                chunk = s.recv(8192)
                if not chunk:
                    break
                buf += chunk
        head, _, raw_body = buf.partition(b"\r\n\r\n")
        assert b"content-encoding: gzip" in head.lower()
        assert gzip.decompress(raw_body) == body


# --- Streaming eligibility (unit) ----------------------------------------


class TestStreamingEligible:
    def test_basic_yes(self):
        r = _r(headers=[(b"content-type", b"text/event-stream")])
        assert _is_streaming_eligible(r, method=b"GET", compressible_types=DEFAULT_COMPRESSIBLE_TYPES)

    def test_head_no(self):
        r = _r(headers=[(b"content-type", b"text/event-stream")])
        assert not _is_streaming_eligible(r, method=b"HEAD", compressible_types=DEFAULT_COMPRESSIBLE_TYPES)

    def test_known_length_no(self):
        # Handler declared exact size — must not compress mid-stream.
        r = _r(headers=[(b"content-type", b"text/event-stream"), (b"content-length", b"100")])
        assert not _is_streaming_eligible(r, method=b"GET", compressible_types=DEFAULT_COMPRESSIBLE_TYPES)

    def test_no_transform_no(self):
        r = _r(headers=[(b"content-type", b"text/event-stream"), (b"cache-control", b"no-transform")])
        assert not _is_streaming_eligible(r, method=b"GET", compressible_types=DEFAULT_COMPRESSIBLE_TYPES)

    def test_existing_encoding_no(self):
        r = _r(headers=[(b"content-type", b"text/event-stream"), (b"content-encoding", b"gzip")])
        assert not _is_streaming_eligible(r, method=b"GET", compressible_types=DEFAULT_COMPRESSIBLE_TYPES)

    def test_non_compressible_type_no(self):
        r = _r(headers=[(b"content-type", b"image/png")])
        assert not _is_streaming_eligible(r, method=b"GET", compressible_types=DEFAULT_COMPRESSIBLE_TYPES)

    @pytest.mark.parametrize("status", [100, 199, 204, 304, 206])
    def test_no_body_status(self, status):
        r = _r(status, headers=[(b"content-type", b"text/event-stream")])
        assert not _is_streaming_eligible(r, method=b"GET", compressible_types=DEFAULT_COMPRESSIBLE_TYPES)


# --- Streaming header rewrite (unit) -------------------------------------


class TestStreamingRewriteHeaders:
    def test_drops_content_length(self):
        out = _streaming_rewrite_headers(
            [(b"content-type", b"text/event-stream"), (b"content-length", b"100")],
            encoding="gzip",
        )
        names = [n for n, _ in out]
        assert b"content-length" not in names

    def test_does_not_add_transfer_encoding(self):
        # Backends auto-frame chunked on HTTP/1.1 — adding TE here breaks
        # httptools' _chunked detection. See _streaming_rewrite_headers' docstring.
        out = _streaming_rewrite_headers([(b"content-type", b"text/event-stream")], encoding="gzip")
        names = [n.lower() for n, _ in out]
        assert b"transfer-encoding" not in names

    def test_adds_content_encoding(self):
        out = _streaming_rewrite_headers([(b"content-type", b"text/event-stream")], encoding="br")
        assert (b"content-encoding", b"br") in out

    def test_merges_vary(self):
        out = _streaming_rewrite_headers(
            [(b"content-type", b"text/event-stream"), (b"vary", b"Cookie")],
            encoding="gzip",
        )
        d = dict(out)
        assert d[b"vary"] == b"Cookie, Accept-Encoding"


# --- Streaming round-trip ------------------------------------------------


def _sse_handler(events: list[bytes]):
    """Handler that emits each item in ``events`` via ``ctx.stream(...)`` —
    the iterator-wrapping path the compression middleware intercepts.
    """

    def handler(ctx: HTTPReqCtx) -> None:
        def chunks() -> Iterator[bytes]:
            for ev in events:
                yield b"data: " + ev + b"\n\n"

        # No Content-Length → streaming path eligible for compression.
        ctx.stream(
            Response(status_code=200, headers=[(b"content-type", b"text/event-stream")]),
            chunks(),
        )

    return handler


class TestStreamingRoundTripGzip:
    def test_sse_compressed(self, serve_backend_in_thread):
        events = [b"hello", b"world", b"x" * 4096, b"final"]
        h = compress_handler(_sse_handler(events), algorithms=("gzip",))
        with serve_backend_in_thread(h) as port:
            r = httpx.get(
                f"http://127.0.0.1:{port}/",
                headers={"accept-encoding": "gzip"},
                timeout=5,
            )
        assert r.status_code == 200
        assert r.headers["content-encoding"] == "gzip"
        assert "transfer-encoding" in r.headers
        # httpx auto-decodes; verify all events present in order.
        decoded = r.content
        for ev in events:
            assert b"data: " + ev + b"\n\n" in decoded
        # Total length ordering preserved.
        positions = [decoded.find(b"data: " + ev) for ev in events]
        assert positions == sorted(positions)

    def test_streaming_passthrough_when_content_length_set(self, serve_backend_in_thread):
        # If the handler set Content-Length, we must not compress (would
        # corrupt the contract). Pass through verbatim.
        body_chunk = b"x" * 4096

        def handler(ctx: HTTPReqCtx) -> None:
            def chunks() -> Iterator[bytes]:
                yield body_chunk

            ctx.stream(
                Response(
                    status_code=200,
                    headers=[
                        (b"content-type", b"text/plain"),
                        (b"content-length", str(len(body_chunk)).encode("ascii")),
                    ],
                ),
                chunks(),
            )

        h = compress_handler(handler, algorithms=("gzip",))
        with serve_backend_in_thread(h) as port:
            r = httpx.get(
                f"http://127.0.0.1:{port}/",
                headers={"accept-encoding": "gzip"},
                timeout=5,
            )
        assert r.status_code == 200
        assert "content-encoding" not in r.headers
        assert r.content == body_chunk

    def test_streaming_passthrough_when_no_transform(self, serve_backend_in_thread):
        events = [b"a", b"b", b"c"]

        def handler(ctx: HTTPReqCtx) -> None:
            def chunks() -> Iterator[bytes]:
                for ev in events:
                    yield b"data: " + ev + b"\n\n"

            ctx.stream(
                Response(
                    status_code=200,
                    headers=[
                        (b"content-type", b"text/event-stream"),
                        (b"cache-control", b"no-transform"),
                    ],
                ),
                chunks(),
            )

        h = compress_handler(handler, algorithms=("gzip",))
        with serve_backend_in_thread(h) as port:
            r = httpx.get(
                f"http://127.0.0.1:{port}/",
                headers={"accept-encoding": "gzip"},
                timeout=5,
            )
        assert "content-encoding" not in r.headers
        assert b"data: a" in r.content


@pytest.mark.skipif(not _HAS_BROTLI, reason="brotli optional extra not installed")
class TestStreamingRoundTripBrotli:
    def test_sse_compressed(self, serve_backend_in_thread):
        events = [b"hello", b"world", b"x" * 4096]
        h = compress_handler(_sse_handler(events), algorithms=("br",))
        with serve_backend_in_thread(h) as port:
            r = httpx.get(
                f"http://127.0.0.1:{port}/",
                headers={"accept-encoding": "br"},
                timeout=5,
            )
        assert r.status_code == 200
        assert r.headers["content-encoding"] == "br"
        for ev in events:
            assert b"data: " + ev in r.content


# --- Per-event flushing (raw socket) -------------------------------------


class TestStreamingFlush:
    """Verify SYNC_FLUSH semantics: the first event must be decodable
    *before* the handler emits the second event. If we relied on the
    full-stream end-flush, the test would deadlock — the handler is
    holding a gate that only releases once the test reads the first event.
    """

    def test_first_event_decodes_before_stream_ends(self, serve_backend_in_thread):
        import socket  # noqa: PLC0415
        import threading  # noqa: PLC0415

        gate = threading.Event()

        def handler(ctx: HTTPReqCtx) -> None:
            def chunks() -> Iterator[bytes]:
                yield b"data: first\n\n"
                # Block until the test has read+decoded "data: first". If
                # SYNC_FLUSH didn't happen, this deadlocks at the 5s timeout.
                gate.wait(timeout=5)
                yield b"data: second\n\n"

            ctx.stream(
                Response(status_code=200, headers=[(b"content-type", b"text/event-stream")]),
                chunks(),
            )

        h = compress_handler(handler, algorithms=("gzip",))
        with serve_backend_in_thread(h) as port, socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nAccept-Encoding: gzip\r\nConnection: close\r\n\r\n")
            buf = bytearray()
            s.settimeout(5)
            while b"\r\n\r\n" not in buf:
                buf.extend(s.recv(8192))
            head, _, after = bytes(buf).partition(b"\r\n\r\n")
            assert b"content-encoding: gzip" in head.lower()
            assert b"transfer-encoding: chunked" in head.lower()

            decompressor = zlib.decompressobj(wbits=31)
            decoded = bytearray()
            stream = bytearray(after)

            while b"data: first" not in decoded:
                consumed = _consume_one_chunk(stream)
                if consumed is None:
                    stream.extend(s.recv(8192))
                    continue
                decoded.extend(decompressor.decompress(consumed))

            assert b"data: first" in decoded
            gate.set()
            # Drain remaining bytes (best-effort) so server-side loop
            # logging stays clean for other tests.
            try:
                while s.recv(8192):
                    pass
            except OSError:
                pass


def _consume_one_chunk(stream: bytearray) -> bytes | None:
    """Pop one HTTP/1.1 chunked-transfer chunk from ``stream``. Returns
    the chunk's payload bytes, or ``None`` if a full chunk isn't present
    yet (caller should ``recv`` more).
    """
    idx = stream.find(b"\r\n")
    if idx <= 0:
        return None
    try:
        size = int(stream[:idx], 16)
    except ValueError:
        return None
    if size == 0:
        return None
    start = idx + 2
    end = start + size
    if len(stream) < end + 2:
        return None
    payload = bytes(stream[start:end])
    del stream[: end + 2]
    return payload
