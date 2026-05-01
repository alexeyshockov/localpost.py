"""Property-based parity tests across the h11 and httptools parser backends.

The README explicitly does NOT unify the two backends behind a Protocol — they
are separate implementations populating the same neutral ``Request`` shape.
Parity is the contract; these tests fuzz wire bytes through both backends and
assert each backend parses the same input into an identical ``Request``.

Companion to ``tests/http/backend_parity.py``, which holds the example-based
parity coverage (lifecycle, framing, error paths). Here we exercise the
shape of the parsed request itself across a much broader input space.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from typing import Literal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from localpost.http import (
    HTTPReqCtx,
    NativeResponse,
    Request,
    RequestHandler,
    ServerConfig,
    start_http_server,
)
from tests.http._helpers import read_http_response

# --- Strategies for HTTP/1.1 wire bytes -------------------------------------

# httptools rejects lowercase methods at parse time (server_httptools.py:249);
# h11 case-folds inline (server_h11.py:236-237). Keep methods uppercase so the
# property compares the post-normalisation shape rather than this divergence.
_method = st.sampled_from([b"GET", b"POST", b"PUT", b"DELETE", b"PATCH", b"HEAD"])

# Chars that obscure path/query splitting or trigger pct-decode questions
# neither parser promises to normalise. Both backends accept them, but we
# strip them from the path strategy to keep the property tractable.
_PATH_UNSAFE = "/{}?#&= %"

_path_segment = st.text(
    st.characters(min_codepoint=0x21, max_codepoint=0x7E, blacklist_characters=_PATH_UNSAFE),
    min_size=1,
    max_size=8,
).map(lambda s: s.encode("ascii"))

_query_token = st.text(
    st.characters(min_codepoint=0x21, max_codepoint=0x7E, blacklist_characters=_PATH_UNSAFE),
    min_size=1,
    max_size=8,
).map(lambda s: s.encode("ascii"))

_header_name = st.from_regex(r"\A[a-zA-Z][a-zA-Z0-9\-]{0,15}\Z").map(lambda s: s.encode("ascii"))

_header_value = st.text(
    st.characters(min_codepoint=0x20, max_codepoint=0x7E, blacklist_characters=":"),
    min_size=0,
    max_size=20,
).map(lambda s: s.encode("ascii"))

# Headers that affect framing or connection lifecycle are excluded — they have
# their own example-based parity coverage in ``backend_parity.py``.
_FRAMING_HEADERS = frozenset(
    (b"host", b"content-length", b"transfer-encoding", b"connection", b"expect", b"upgrade", b"trailer", b"te"),
)


@st.composite
def _path(draw) -> bytes:
    n = draw(st.integers(min_value=1, max_value=4))
    return b"/" + b"/".join(draw(_path_segment) for _ in range(n))


@st.composite
def _query(draw) -> bytes:
    n = draw(st.integers(min_value=0, max_value=3))
    if n == 0:
        return b""
    pairs = [draw(_query_token) + b"=" + draw(_query_token) for _ in range(n)]
    return b"?" + b"&".join(pairs)


@st.composite
def _extra_headers(draw) -> list[tuple[bytes, bytes]]:
    n = draw(st.integers(min_value=0, max_value=4))
    out: list[tuple[bytes, bytes]] = []
    seen: set[bytes] = set()
    for _ in range(n):
        name = draw(_header_name)
        key = name.lower()
        if key in _FRAMING_HEADERS or key in seen:
            continue
        seen.add(key)
        out.append((name, draw(_header_value)))
    return out


@st.composite
def _request_wire(draw) -> tuple[bytes, dict]:
    """Build (wire_bytes, expected_shape) for a fuzzed HTTP/1.1 request.

    The expected shape is the normalised ``Request`` projection both backends
    must produce — methods uppercase, header names lowercase, path/query
    pre-split on the first ``?``.
    """
    method = draw(_method)
    path = draw(_path())
    query = draw(_query())
    extras = draw(_extra_headers())

    target = path + query
    headers = [(b"Host", b"example.com"), *extras]
    request_line = method + b" " + target + b" HTTP/1.1\r\n"
    header_block = b"".join(name + b": " + value + b"\r\n" for name, value in headers)
    wire = request_line + header_block + b"\r\n"

    expected = {
        "method": method,
        "target": target,
        "path": path,
        "query_string": query[1:] if query else b"",
        "headers": [(name.lower(), value) for name, value in headers],
        "http_version": b"1.1",
    }
    return wire, expected


# --- Server scaffolding -----------------------------------------------------


@contextmanager
def _serve(handler: RequestHandler, backend: Literal["h11", "httptools"]) -> Generator[int]:
    """Start an HTTP server on ``backend`` in a background thread; yield its port."""
    cfg = ServerConfig(host="127.0.0.1", port=0, backend=backend)
    cm = start_http_server(cfg, handler)
    server = cm.__enter__()
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            try:
                server.run(timeout=0.05)
            except OSError:
                return

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    try:
        yield server.port
    finally:
        stop.set()
        t.join(timeout=5)
        cm.__exit__(None, None, None)


@pytest.fixture
def parity_servers() -> Iterator[tuple[int, int, list[Request], list[Request]]]:
    """Run h11 + httptools servers in parallel; both capture their parsed requests."""
    pytest.importorskip("httptools")

    h11_captured: list[Request] = []
    ht_captured: list[Request] = []

    def make_handler(captured: list[Request]) -> RequestHandler:
        def handler(ctx: HTTPReqCtx):
            captured.append(ctx.request)
            ctx.complete(NativeResponse(200, [(b"content-length", b"0")]), b"")

        return handler

    with (
        _serve(make_handler(h11_captured), "h11") as h11_port,
        _serve(make_handler(ht_captured), "httptools") as ht_port,
    ):
        yield h11_port, ht_port, h11_captured, ht_captured


def _send_one(port: int, wire: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
        sock.sendall(wire)
        return read_http_response(sock)


def _shape(req: Request) -> dict:
    return {
        "method": req.method,
        "target": req.target,
        "path": req.path,
        "query_string": req.query_string,
        "headers": list(req.headers),
        "http_version": req.http_version,
    }


# --- Properties -------------------------------------------------------------


class TestParserParity:
    @given(payload=_request_wire())
    @settings(
        max_examples=80,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_request_shape_parity(self, parity_servers, payload):
        """Both backends parse the same wire bytes into equal ``Request`` shapes."""
        h11_port, ht_port, h11_captured, ht_captured = parity_servers
        h11_captured.clear()
        ht_captured.clear()

        wire, expected = payload
        h11_resp = _send_one(h11_port, wire)
        ht_resp = _send_one(ht_port, wire)

        # Both backends must respond — confirms the wire was accepted.
        assert b"HTTP/1.1 200" in h11_resp, h11_resp
        assert b"HTTP/1.1 200" in ht_resp, ht_resp

        assert len(h11_captured) == 1, h11_captured
        assert len(ht_captured) == 1, ht_captured

        h11_shape = _shape(h11_captured[0])
        ht_shape = _shape(ht_captured[0])

        # Cross-backend parity.
        assert h11_shape == ht_shape
        # And both match the spec we built the wire from — locks intent, not
        # just agreement-on-a-bug.
        assert h11_shape == expected
