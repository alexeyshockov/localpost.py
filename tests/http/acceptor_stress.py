"""Stress tests for the acceptor + multi-selector topology.

The recent acceptor refactor (``selectors=N, acceptor=True``) introduced a
dedicated acceptor thread that round-robins fresh conns onto N worker
selectors via ``Selector.post_track`` (cross-thread op queue + wakeup pipe).
Existing example tests in ``service.py`` cover the basic happy paths;
these tests push harder on:

- exact distribution under burst (counter is single-threaded → deterministic)
- no conn loss when many clients connect concurrently
- keep-alive conns stay pinned to their initial worker
- clean shutdown leaves no orphan listening socket (port can be re-bound)
"""

from __future__ import annotations

import socket
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest
from anyio import to_thread

from localpost.hosting import serve
from localpost.http import (
    BodyHandler,
    HTTPReqCtx,
    NativeResponse,
    ServerConfig,
    http_server,
)
from tests.http._helpers import read_http_response

pytestmark = pytest.mark.anyio


def _capturing_handler(seen: Counter, lock: threading.Lock):
    def handler(ctx: HTTPReqCtx) -> BodyHandler | None:
        with lock:
            seen[threading.get_ident()] += 1
        ctx.complete(
            NativeResponse(status_code=200, headers=[(b"content-length", b"2")]),
            b"ok",
        )
        return None

    return handler


def _wait_ready(port: int, deadline: float = 5.0) -> None:
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"server on port {port} not ready within {deadline}s")


def _request_close(port: int) -> bytes:
    """Open a fresh TCP conn, send GET / with Connection: close, return response bytes."""
    with socket.create_connection(("127.0.0.1", port), timeout=5.0) as sock:
        sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        return read_http_response(sock, deadline=5.0)


class TestRoundRobinDistribution:
    async def test_exact_round_robin_with_fresh_conns(self, free_port):
        """N=4 workers, K=N*10 fresh conns: each worker handles exactly 10.

        ``RoundRobinAcceptor`` runs single-threaded on the acceptor thread,
        so its ``_next`` counter increments deterministically. With every
        client opening a brand-new TCP conn, each ``accept()`` is a fresh
        round-robin assignment.
        """
        seen: Counter = Counter()
        lock = threading.Lock()
        n_workers = 4
        n_conns = n_workers * 10

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with serve(
            http_server(cfg, _capturing_handler(seen, lock), selectors=n_workers, acceptor=True),
        ) as lt:
            await lt.started
            await to_thread.run_sync(_wait_ready, free_port)

            def fire_all() -> list[bytes]:
                with ThreadPoolExecutor(max_workers=n_conns) as ex:
                    futures = [ex.submit(_request_close, free_port) for _ in range(n_conns)]
                    return [f.result() for f in futures]

            responses = await to_thread.run_sync(fire_all)

            for r in responses:
                assert b"HTTP/1.1 200" in r, r

            lt.shutdown()
            await lt.stopped

        assert lt.exit_code == 0
        # Every worker selector thread must have served exactly n_conns/n_workers requests.
        assert len(seen) == n_workers, seen
        assert all(count == n_conns // n_workers for count in seen.values()), seen


class TestBurst:
    async def test_no_conn_loss_under_burst(self, free_port):
        """64 concurrent fresh conns through a 4-worker acceptor: every one returns 200.

        No assertion on distribution — the kernel ``accept()`` queue can
        reorder under heavy concurrent connect()s, and SYN-flood mitigation
        on the loopback may drop conns under extreme load. The point is no
        request silently disappears or hangs.
        """
        seen: Counter = Counter()
        lock = threading.Lock()
        n_conns = 64

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with serve(
            http_server(cfg, _capturing_handler(seen, lock), selectors=4, acceptor=True),
        ) as lt:
            await lt.started
            await to_thread.run_sync(_wait_ready, free_port)

            def fire_all() -> list[bytes]:
                with ThreadPoolExecutor(max_workers=n_conns) as ex:
                    futures = [ex.submit(_request_close, free_port) for _ in range(n_conns)]
                    return [f.result() for f in futures]

            responses = await to_thread.run_sync(fire_all)

            for r in responses:
                assert b"HTTP/1.1 200" in r, r

            assert sum(seen.values()) == n_conns, seen

            lt.shutdown()
            await lt.stopped

        assert lt.exit_code == 0


class TestKeepAlivePinning:
    async def test_keepalive_conn_stays_on_one_worker(self, free_port):
        """A single keep-alive conn issuing N requests is served by ONE worker.

        A conn is round-robined once on accept; afterwards it lives on that
        worker's selector for its whole lifetime. Verifying this matters
        because a stray re-routing would corrupt the parser handoff
        invariant (parser owned by exactly one selector).
        """
        seen: Counter = Counter()
        lock = threading.Lock()
        n_requests = 5

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with serve(
            http_server(cfg, _capturing_handler(seen, lock), selectors=4, acceptor=True),
        ) as lt:
            await lt.started
            await to_thread.run_sync(_wait_ready, free_port)

            def hammer_one_socket() -> list[bytes]:
                with socket.create_connection(("127.0.0.1", free_port), timeout=5.0) as sock:
                    out = []
                    for _ in range(n_requests):
                        sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
                        out.append(read_http_response(sock, deadline=5.0))
                    return out

            responses = await to_thread.run_sync(hammer_one_socket)
            for r in responses:
                assert b"HTTP/1.1 200" in r, r

            lt.shutdown()
            await lt.stopped

        # All N requests landed on the same worker selector thread.
        assert len(seen) == 1, seen
        assert next(iter(seen.values())) == n_requests, seen


class TestShutdownReleasesPort:
    async def test_port_re_bindable_after_shutdown(self, free_port):
        """After ``lt.stopped``, the listening socket is fully released — the
        same port can be re-bound immediately. This is a stronger leak check
        than ``exit_code == 0``: it proves the kernel-level fd is freed."""
        seen: Counter = Counter()
        lock = threading.Lock()

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with serve(
            http_server(cfg, _capturing_handler(seen, lock), selectors=2, acceptor=True),
        ) as lt:
            await lt.started
            await to_thread.run_sync(_wait_ready, free_port)

            def fire() -> bytes:
                return _request_close(free_port)

            for _ in range(3):
                resp = await to_thread.run_sync(fire)
                assert b"HTTP/1.1 200" in resp, resp

            lt.shutdown()
            await lt.stopped

        assert lt.exit_code == 0

        # Re-bind the same port to confirm the listening fd is released.
        # ``SO_REUSEADDR`` clears noise from per-conn sockets that are still
        # in ``TIME_WAIT`` (server actively closed them via ``Connection:
        # close``); it does NOT bypass an active bind, so the test still
        # fails if we leaked the listening fd.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", free_port))
            probe.listen(1)
