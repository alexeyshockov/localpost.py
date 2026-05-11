"""Tests for localpost.http.static — file serving via socket.sendfile()."""

from __future__ import annotations

import os
import time
from email.utils import formatdate
from pathlib import Path

import httpx
import pytest

from localpost.http import static_handler
from localpost.http.static import (
    _etag,
    _if_modified_since_satisfied,
    _if_none_match_matches,
    _parse_range,
    _resolve,
)

# --- Helpers (unit) ------------------------------------------------------


class TestResolve:
    def test_simple(self, tmp_path: Path):
        (tmp_path / "a.txt").write_bytes(b"x")
        assert _resolve(tmp_path, b"a.txt", index=None) == tmp_path / "a.txt"

    def test_leading_slash_stripped(self, tmp_path: Path):
        (tmp_path / "a.txt").write_bytes(b"x")
        assert _resolve(tmp_path, b"/a.txt", index=None) == tmp_path / "a.txt"

    def test_percent_decode(self, tmp_path: Path):
        (tmp_path / "a b.txt").write_bytes(b"x")
        assert _resolve(tmp_path, b"a%20b.txt", index=None) == tmp_path / "a b.txt"

    def test_traversal_dotdot_segment_rejected(self, tmp_path: Path):
        # Even before resolve(), explicit ``..`` segments are rejected.
        assert _resolve(tmp_path, b"../etc/passwd", index=None) is None
        assert _resolve(tmp_path, b"foo/../../etc/passwd", index=None) is None

    def test_traversal_via_encoded_dotdot(self, tmp_path: Path):
        # %2e%2e decodes to ``..`` — caught by the segment check.
        assert _resolve(tmp_path, b"%2e%2e/etc/passwd", index=None) is None

    def test_null_byte_rejected(self, tmp_path: Path):
        assert _resolve(tmp_path, b"a\x00b", index=None) is None

    def test_invalid_utf8_rejected(self, tmp_path: Path):
        # %ff is not valid UTF-8.
        assert _resolve(tmp_path, b"%ff.txt", index=None) is None

    def test_directory_with_index(self, tmp_path: Path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "index.html").write_bytes(b"x")
        assert _resolve(tmp_path, b"sub/", index="index.html") == tmp_path / "sub" / "index.html"
        assert _resolve(tmp_path, b"sub", index="index.html") == tmp_path / "sub" / "index.html"

    def test_directory_no_index(self, tmp_path: Path):
        (tmp_path / "sub").mkdir()
        assert _resolve(tmp_path, b"sub/", index=None) is None

    def test_directory_index_disabled_when_missing(self, tmp_path: Path):
        # Index resolution returns the joined path even when the index file
        # does not exist; caller stats and 404s.
        (tmp_path / "sub").mkdir()
        result = _resolve(tmp_path, b"sub/", index="index.html")
        assert result is not None
        assert result == tmp_path / "sub" / "index.html"
        assert not result.exists()


class TestETag:
    def test_shape(self, tmp_path: Path):
        p = tmp_path / "a.txt"
        p.write_bytes(b"hello")
        st = p.stat()
        tag = _etag(st)
        assert tag.startswith(b'"')
        assert tag.endswith(b'"')
        # Stable across re-stats of the same file.
        assert _etag(p.stat()) == tag


class TestIfNoneMatch:
    def test_strong_match(self):
        assert _if_none_match_matches(b'"abc"', b'"abc"')

    def test_no_match(self):
        assert not _if_none_match_matches(b'"abc"', b'"xyz"')

    def test_wildcard(self):
        assert _if_none_match_matches(b"*", b'"anything"')

    def test_weak_match(self):
        assert _if_none_match_matches(b'W/"abc"', b'"abc"')
        assert _if_none_match_matches(b'"abc"', b'W/"abc"')

    def test_multi_value(self):
        assert _if_none_match_matches(b'"x", "abc", "y"', b'"abc"')
        assert not _if_none_match_matches(b'"x", "y"', b'"abc"')

    def test_whitespace_tolerant(self):
        assert _if_none_match_matches(b'  "abc" , "y"', b'"abc"')


class TestIfModifiedSince:
    def test_after(self):
        # File modified at t=1000, header says "since 2000" → not modified.
        assert _if_modified_since_satisfied(b"Thu, 01 Jan 1970 00:00:01 GMT", 0.0)

    def test_before(self):
        # File modified now, header says "since the past" → modified.
        past = formatdate(time.time() - 3600, usegmt=True).encode("ascii")
        assert not _if_modified_since_satisfied(past, time.time())

    def test_invalid(self):
        assert not _if_modified_since_satisfied(b"not a date", 100.0)


class TestParseRange:
    def test_full_range(self):
        assert _parse_range(b"bytes=0-99", 1000) == (0, 100)

    def test_mid_range(self):
        assert _parse_range(b"bytes=100-199", 1000) == (100, 100)

    def test_open_ended(self):
        assert _parse_range(b"bytes=500-", 1000) == (500, 500)

    def test_suffix(self):
        assert _parse_range(b"bytes=-50", 1000) == (950, 50)

    def test_suffix_larger_than_size(self):
        # Per RFC 7233 §2.1, clamped to full content.
        assert _parse_range(b"bytes=-5000", 1000) == (0, 1000)

    def test_end_clamped(self):
        # End past EOF clamps to size-1.
        assert _parse_range(b"bytes=100-9999", 1000) == (100, 900)

    def test_start_past_eof_unsatisfiable(self):
        assert _parse_range(b"bytes=2000-2999", 1000) == "unsatisfiable"

    def test_zero_size_unsatisfiable(self):
        assert _parse_range(b"bytes=0-99", 0) == "unsatisfiable"

    def test_multi_range_falls_back(self):
        # Multi-range → 200 fallback (we don't speak multipart/byteranges).
        assert _parse_range(b"bytes=0-99,200-299", 1000) is None

    def test_unparsable(self):
        assert _parse_range(b"bytes=abc-def", 1000) is None
        assert _parse_range(b"bytes=", 1000) is None
        assert _parse_range(b"items=0-99", 1000) is None
        assert _parse_range(b"bytes=10-5", 1000) is None  # end < start

    def test_case_insensitive_unit(self):
        assert _parse_range(b"BYTES=0-9", 100) == (0, 10)


# --- Integration ---------------------------------------------------------


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A populated directory served by all integration tests."""
    (tmp_path / "hello.txt").write_bytes(b"Hello, world!\n")
    (tmp_path / "page.html").write_bytes(b"<html></html>")
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    (tmp_path / "weird.zzzz").write_bytes(b"unknown extension")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "index.html").write_bytes(b"sub-index")
    return tmp_path


class TestStaticBasic:
    def test_get_200(self, root: Path, serve_backend_in_thread):
        h = static_handler(root)
        with serve_backend_in_thread(h) as port:
            r = httpx.get(f"http://127.0.0.1:{port}/hello.txt", timeout=5)
        assert r.status_code == 200
        assert r.content == b"Hello, world!\n"
        assert r.headers["content-type"] == "text/plain"
        assert r.headers["content-length"] == "14"
        assert r.headers["accept-ranges"] == "bytes"
        assert r.headers["etag"].startswith('"')
        assert "last-modified" in r.headers

    def test_404_missing(self, root: Path, serve_backend_in_thread):
        h = static_handler(root)
        with serve_backend_in_thread(h) as port:
            r = httpx.get(f"http://127.0.0.1:{port}/nope", timeout=5)
        assert r.status_code == 404

    def test_404_directory_traversal(self, root: Path, serve_backend_in_thread):
        h = static_handler(root)
        with serve_backend_in_thread(h) as port:
            # httpx normalises paths; use raw URL bytes via build_request.
            req = httpx.Request("GET", f"http://127.0.0.1:{port}/../etc/passwd")
            with httpx.Client(timeout=5) as client:
                r = client.send(req)
        # Whatever path httpx ends up sending, must not leak content outside root.
        assert r.status_code in (400, 404)

    def test_405_wrong_method(self, root: Path, serve_backend_in_thread):
        h = static_handler(root)
        with serve_backend_in_thread(h) as port:
            r = httpx.put(f"http://127.0.0.1:{port}/hello.txt", content=b"x", timeout=5)
        assert r.status_code == 405
        assert r.headers["allow"] == "GET, HEAD"

    def test_prefix_stripped(self, root: Path, serve_backend_in_thread):
        h = static_handler(root, prefix=b"/static/")
        with serve_backend_in_thread(h) as port:
            r = httpx.get(f"http://127.0.0.1:{port}/static/hello.txt", timeout=5)
            r_miss = httpx.get(f"http://127.0.0.1:{port}/hello.txt", timeout=5)
        assert r.status_code == 200
        assert r.content == b"Hello, world!\n"
        assert r_miss.status_code == 404

    def test_index_directory(self, root: Path, serve_backend_in_thread):
        h = static_handler(root)
        with serve_backend_in_thread(h) as port:
            r = httpx.get(f"http://127.0.0.1:{port}/sub/", timeout=5)
        assert r.status_code == 200
        assert r.content == b"sub-index"

    def test_index_disabled(self, root: Path, serve_backend_in_thread):
        h = static_handler(root, index=None)
        with serve_backend_in_thread(h) as port:
            r = httpx.get(f"http://127.0.0.1:{port}/sub/", timeout=5)
        assert r.status_code == 404


class TestStaticHead:
    def test_head_200(self, root: Path, serve_backend_in_thread):
        h = static_handler(root)
        with serve_backend_in_thread(h) as port:
            r = httpx.head(f"http://127.0.0.1:{port}/hello.txt", timeout=5)
        assert r.status_code == 200
        assert r.content == b""
        assert r.headers["content-length"] == "14"
        assert r.headers["etag"].startswith('"')

    def test_head_404(self, root: Path, serve_backend_in_thread):
        h = static_handler(root)
        with serve_backend_in_thread(h) as port:
            r = httpx.head(f"http://127.0.0.1:{port}/nope", timeout=5)
        assert r.status_code == 404


class TestStaticConditionals:
    def test_if_none_match_hit_returns_304(self, root: Path, serve_backend_in_thread):
        h = static_handler(root)
        with serve_backend_in_thread(h) as port:
            base = f"http://127.0.0.1:{port}/hello.txt"
            r1 = httpx.get(base, timeout=5)
            assert r1.status_code == 200
            etag = r1.headers["etag"]
            r2 = httpx.get(base, headers={"if-none-match": etag}, timeout=5)
        assert r2.status_code == 304
        assert r2.content == b""
        assert r2.headers["etag"] == etag

    def test_if_none_match_miss_returns_200(self, root: Path, serve_backend_in_thread):
        h = static_handler(root)
        with serve_backend_in_thread(h) as port:
            r = httpx.get(
                f"http://127.0.0.1:{port}/hello.txt",
                headers={"if-none-match": '"deadbeef"'},
                timeout=5,
            )
        assert r.status_code == 200

    def test_if_modified_since_after_mtime(self, root: Path, serve_backend_in_thread):
        h = static_handler(root)
        # Set mtime to a known past second so the comparison is deterministic.
        target = root / "hello.txt"
        os.utime(target, (1_000_000, 1_000_000))
        with serve_backend_in_thread(h) as port:
            r = httpx.get(
                f"http://127.0.0.1:{port}/hello.txt",
                headers={"if-modified-since": formatdate(2_000_000, usegmt=True)},
                timeout=5,
            )
        assert r.status_code == 304

    def test_if_modified_since_before_mtime(self, root: Path, serve_backend_in_thread):
        h = static_handler(root)
        target = root / "hello.txt"
        os.utime(target, (2_000_000, 2_000_000))
        with serve_backend_in_thread(h) as port:
            r = httpx.get(
                f"http://127.0.0.1:{port}/hello.txt",
                headers={"if-modified-since": formatdate(1_000_000, usegmt=True)},
                timeout=5,
            )
        assert r.status_code == 200


class TestStaticRange:
    def test_prefix_range(self, root: Path, serve_backend_in_thread):
        h = static_handler(root)
        with serve_backend_in_thread(h) as port:
            r = httpx.get(
                f"http://127.0.0.1:{port}/hello.txt",
                headers={"range": "bytes=0-4"},
                timeout=5,
            )
        assert r.status_code == 206
        assert r.content == b"Hello"
        assert r.headers["content-length"] == "5"
        assert r.headers["content-range"] == "bytes 0-4/14"

    def test_mid_range(self, root: Path, serve_backend_in_thread):
        h = static_handler(root)
        with serve_backend_in_thread(h) as port:
            r = httpx.get(
                f"http://127.0.0.1:{port}/hello.txt",
                headers={"range": "bytes=7-11"},
                timeout=5,
            )
        assert r.status_code == 206
        assert r.content == b"world"
        assert r.headers["content-range"] == "bytes 7-11/14"

    def test_suffix_range(self, root: Path, serve_backend_in_thread):
        h = static_handler(root)
        with serve_backend_in_thread(h) as port:
            r = httpx.get(
                f"http://127.0.0.1:{port}/hello.txt",
                headers={"range": "bytes=-7"},
                timeout=5,
            )
        assert r.status_code == 206
        assert r.content == b"world!\n"

    def test_unsatisfiable_range(self, root: Path, serve_backend_in_thread):
        h = static_handler(root)
        with serve_backend_in_thread(h) as port:
            r = httpx.get(
                f"http://127.0.0.1:{port}/hello.txt",
                headers={"range": "bytes=9999-"},
                timeout=5,
            )
        assert r.status_code == 416
        assert r.headers["content-range"] == "bytes */14"

    def test_invalid_range_falls_back_to_200(self, root: Path, serve_backend_in_thread):
        h = static_handler(root)
        with serve_backend_in_thread(h) as port:
            r = httpx.get(
                f"http://127.0.0.1:{port}/hello.txt",
                headers={"range": "bytes=abc-def"},
                timeout=5,
            )
        assert r.status_code == 200
        assert r.content == b"Hello, world!\n"

    def test_multi_range_falls_back_to_200(self, root: Path, serve_backend_in_thread):
        h = static_handler(root)
        with serve_backend_in_thread(h) as port:
            r = httpx.get(
                f"http://127.0.0.1:{port}/hello.txt",
                headers={"range": "bytes=0-4,7-11"},
                timeout=5,
            )
        assert r.status_code == 200
        assert r.content == b"Hello, world!\n"


class TestStaticContentType:
    def test_html(self, root: Path, serve_backend_in_thread):
        h = static_handler(root)
        with serve_backend_in_thread(h) as port:
            r = httpx.get(f"http://127.0.0.1:{port}/page.html", timeout=5)
        assert r.headers["content-type"] == "text/html"

    def test_png(self, root: Path, serve_backend_in_thread):
        h = static_handler(root)
        with serve_backend_in_thread(h) as port:
            r = httpx.get(f"http://127.0.0.1:{port}/image.png", timeout=5)
        assert r.headers["content-type"] == "image/png"

    def test_unknown_extension_octet_stream(self, root: Path, serve_backend_in_thread):
        h = static_handler(root)
        with serve_backend_in_thread(h) as port:
            r = httpx.get(f"http://127.0.0.1:{port}/weird.zzzz", timeout=5)
        assert r.headers["content-type"] == "application/octet-stream"


class TestStaticCacheControl:
    def test_passthrough_on_200(self, root: Path, serve_backend_in_thread):
        h = static_handler(root, cache_control="public, max-age=3600, immutable")
        with serve_backend_in_thread(h) as port:
            r = httpx.get(f"http://127.0.0.1:{port}/hello.txt", timeout=5)
        assert r.headers["cache-control"] == "public, max-age=3600, immutable"

    def test_passthrough_on_304(self, root: Path, serve_backend_in_thread):
        h = static_handler(root, cache_control="public, max-age=3600")
        with serve_backend_in_thread(h) as port:
            base = f"http://127.0.0.1:{port}/hello.txt"
            etag = httpx.get(base, timeout=5).headers["etag"]
            r = httpx.get(base, headers={"if-none-match": etag}, timeout=5)
        assert r.status_code == 304
        assert r.headers["cache-control"] == "public, max-age=3600"


class TestStaticLargeFile:
    def test_round_trip_1mb(self, tmp_path: Path, serve_backend_in_thread):
        # ≥ 1 MB so socket.sendfile makes multiple os.sendfile iterations.
        payload = os.urandom(1 << 20)
        (tmp_path / "blob.bin").write_bytes(payload)
        h = static_handler(tmp_path)
        with serve_backend_in_thread(h) as port:
            r = httpx.get(f"http://127.0.0.1:{port}/blob.bin", timeout=15)
        assert r.status_code == 200
        assert int(r.headers["content-length"]) == len(payload)
        assert r.content == payload

    def test_range_within_large_file(self, tmp_path: Path, serve_backend_in_thread):
        payload = os.urandom(1 << 20)
        (tmp_path / "blob.bin").write_bytes(payload)
        h = static_handler(tmp_path)
        with serve_backend_in_thread(h) as port:
            r = httpx.get(
                f"http://127.0.0.1:{port}/blob.bin",
                headers={"range": "bytes=100-199"},
                timeout=10,
            )
        assert r.status_code == 206
        assert r.content == payload[100:200]
