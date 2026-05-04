"""Tests for ``localpost.http._service`` (the hosted ``http_server`` service)
and ``localpost.http._pool`` (the ``thread_pool_handler`` async CM).
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from collections import Counter
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import cast

import anyio
import httpx
import pytest
from anyio import to_thread

from localpost.hosting import ServiceLifetimeView, serve
from localpost.http import (
    BodyHandler,
    HTTPReqCtx,
    Request,
    RequestCancelled,
    RequestHandler,
    Response,
    Routes,
    ServerConfig,
    check_cancelled,
    http_server,
    route_match,
    streaming_pool_handler,
    thread_pool_handler,
)
from tests.http._helpers import read_http_response

pytestmark = pytest.mark.anyio


@asynccontextmanager
async def _serve_pooled(
    cfg: ServerConfig,
    handler: RequestHandler,
    *,
    max_concurrency: int,
    backlog: int = 0,
    selectors: int = 1,
    acceptor: bool = False,
) -> AsyncGenerator[ServiceLifetimeView]:
    """Compose ``thread_pool_handler`` + ``http_server`` and yield the http_server lifetime view.

    Tests own shutdown via the yielded lifetime; the pool drains on exit.
    """
    async with thread_pool_handler(handler, max_concurrency=max_concurrency, backlog=backlog) as wrapped:
        async with serve(http_server(cfg, wrapped, selectors=selectors, acceptor=acceptor)) as lt:
            yield lt


def _handler_200(body: bytes = b"ok"):
    def handler(ctx: HTTPReqCtx):
        ctx.complete(
            Response(
                status_code=200,
                headers=[(b"content-type", b"text/plain"), (b"content-length", str(len(body)).encode())],
            ),
            body,
        )

    return handler


async def _get(url: str, **kw) -> httpx.Response:
    """httpx.get from an async test — offload to a worker thread."""
    return await to_thread.run_sync(lambda: httpx.get(url, **kw))


async def _wait_server_ready(port: int, deadline: float = 5.0):
    """Poll the port until it accepts a connection."""

    def probe():
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return True
            except OSError:
                time.sleep(0.05)
        return False

    return await to_thread.run_sync(probe)


class TestHttpServerService:
    async def test_serves_single_request_immediate(self, free_port):
        """Immediate handler: no thread pool, runs on the selector thread."""
        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with serve(http_server(cfg, _handler_200(b"hi"))) as lt:
            await lt.started
            await _wait_server_ready(free_port)
            resp = await _get(f"http://127.0.0.1:{free_port}/")
            assert resp.status_code == 200
            assert resp.text == "hi"
            lt.shutdown()
            await lt.stopped
        assert lt.exit_code == 0

    async def test_serves_single_request_pooled(self, free_port):
        """Pool-wrapped handler: same observable behaviour, but on a worker thread."""
        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with _serve_pooled(cfg, _handler_200(b"hi"), max_concurrency=1) as lt:
            await lt.started
            await _wait_server_ready(free_port)
            resp = await _get(f"http://127.0.0.1:{free_port}/")
            assert resp.status_code == 200
            assert resp.text == "hi"
            lt.shutdown()
            await lt.stopped
        assert lt.exit_code == 0

    async def test_each_request_becomes_a_task(self, free_port):
        """max_concurrency>1 → several slow handlers run in parallel (different threads)."""
        thread_ids: list[int] = []
        lock = threading.Lock()
        entered = threading.Semaphore(0)  # used as a barrier signal
        release = threading.Event()

        def body_handler(ctx: HTTPReqCtx):
            with lock:
                thread_ids.append(threading.get_ident())
            entered.release()
            # Block until the test releases us; this forces parallelism.
            release.wait(timeout=5.0)
            ctx.complete(
                Response(status_code=200, headers=[(b"content-length", b"2")]),
                b"ok",
            )

        def handler(_ctx: HTTPReqCtx):
            return body_handler

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with _serve_pooled(cfg, handler, max_concurrency=4) as lt:
            await lt.started
            await _wait_server_ready(free_port)

            async def fire():
                return await _get(f"http://127.0.0.1:{free_port}/")

            results: list[httpx.Response | None] = [None, None, None]

            async def do(i):
                results[i] = await fire()

            async with anyio.create_task_group() as tg:
                tg.start_soon(do, 0)
                tg.start_soon(do, 1)
                tg.start_soon(do, 2)

                # Wait until all three handlers have entered before releasing.
                def wait_for_three():
                    for _ in range(3):
                        assert entered.acquire(timeout=5.0)

                await to_thread.run_sync(wait_for_three)
                release.set()

            for r in results:
                assert r is not None
                assert r.status_code == 200

            assert len(thread_ids) == 3
            # Three requests served from different worker threads.
            assert len(set(thread_ids)) >= 2  # at least two distinct threads

            lt.shutdown()
            await lt.stopped

    async def test_max_concurrency_one_serializes(self, free_port):
        """With a 1-slot pool plus a queue, all requests run, just one at a time."""
        in_flight = 0
        peak = 0
        lock = threading.Lock()

        def body_handler(ctx: HTTPReqCtx):
            nonlocal in_flight, peak
            with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            time.sleep(0.1)
            with lock:
                in_flight -= 1
            ctx.complete(Response(status_code=200, headers=[(b"content-length", b"2")]), b"ok")

        def handler(_ctx: HTTPReqCtx):
            return body_handler

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with _serve_pooled(cfg, handler, max_concurrency=1, backlog=2) as lt:
            await lt.started
            await _wait_server_ready(free_port)

            async with anyio.create_task_group() as tg:
                for _ in range(3):
                    tg.start_soon(_get, f"http://127.0.0.1:{free_port}/")

            assert peak == 1

            lt.shutdown()
            await lt.stopped

    async def test_shutdown_cancels_inflight(self, free_port):
        """Triggering shutdown while a handler is running cancels it via the HTTP cancellation token.

        ``thread_pool_handler``'s exit sets the shared shutdown event ORed into
        every in-flight ``RequestCancel``, so the next ``check_cancelled`` call
        in the handler raises ``RequestCancelled``.
        """
        handler_started = threading.Event()
        handler_cancelled = threading.Event()

        def body_handler(ctx: HTTPReqCtx):
            handler_started.set()
            try:
                for _ in range(100):
                    check_cancelled()
                    time.sleep(0.05)
            except RequestCancelled:
                handler_cancelled.set()
                raise
            ctx.complete(Response(status_code=200, headers=[(b"content-length", b"2")]), b"ok")

        def handler(_ctx: HTTPReqCtx):
            return body_handler

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with _serve_pooled(cfg, handler, max_concurrency=2) as lt:
            await lt.started
            await _wait_server_ready(free_port)

            # Fire and forget — we don't wait for the response since the handler will be cancelled.
            async def fire_and_forget():
                with contextlib.suppress(Exception):
                    await _get(f"http://127.0.0.1:{free_port}/", timeout=2.0)

            async with anyio.create_task_group() as tg:
                tg.start_soon(fire_and_forget)

                # Wait for the handler to start
                await to_thread.run_sync(lambda: handler_started.wait(5.0))

                lt.shutdown()

            await lt.stopped

        # The handler's own loop observes cancellation via ``check_cancelled``.
        assert handler_cancelled.is_set()

    async def test_router_dispatch_via_service(self, free_port):
        routes = Routes()

        @routes.get("/books/{id}")
        def get_book(ctx: HTTPReqCtx) -> BodyHandler | None:
            book_id = route_match(ctx).path_args["id"]
            body = f"book={book_id}".encode()
            ctx.complete(
                Response(
                    status_code=200,
                    headers=[(b"content-type", b"text/plain"), (b"content-length", str(len(body)).encode("ascii"))],
                ),
                body,
            )
            return None

        assert get_book is not None
        router = routes.build()

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with _serve_pooled(cfg, router.as_handler(), max_concurrency=4) as lt:
            await lt.started
            await _wait_server_ready(free_port)

            resp = await _get(f"http://127.0.0.1:{free_port}/books/42")
            assert resp.status_code == 200
            assert resp.text == "book=42"

            resp = await _get(f"http://127.0.0.1:{free_port}/missing")
            assert resp.status_code == 404

            lt.shutdown()
            await lt.stopped

    async def test_invalid_max_concurrency(self):
        with pytest.raises(ValueError, match="max_concurrency"):
            async with thread_pool_handler(_handler_200(), max_concurrency=0):
                pass

    async def test_invalid_backlog(self):
        with pytest.raises(ValueError, match="backlog"):
            async with thread_pool_handler(_handler_200(), max_concurrency=1, backlog=-1):
                pass


class TestBacklogAdmission:
    """``backlog`` controls the channel buffer between selector and workers.

    Default ``backlog=0`` is rendezvous: the selector's ``put_nowait`` only
    succeeds when a worker is currently waiting on ``Channel.get()``;
    otherwise the request is rejected with 503 immediately. ``backlog=K``
    admits up to ``max_concurrency + K`` requests before 503s start.
    """

    async def test_rendezvous_default_rejects_when_workers_busy(self, free_port):
        """backlog=0, max_concurrency=1: a 2nd concurrent request 503s
        immediately because no worker is waiting on get()."""
        entered = threading.Event()
        release = threading.Event()

        def body_handler(ctx: HTTPReqCtx):
            entered.set()
            release.wait(timeout=5.0)
            ctx.complete(Response(status_code=200, headers=[(b"content-length", b"2")]), b"ok")

        def handler(_ctx: HTTPReqCtx):
            return body_handler

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with _serve_pooled(cfg, handler, max_concurrency=1) as lt:
            await lt.started
            await _wait_server_ready(free_port)

            try:
                # Block the only worker.
                async def slow():
                    return await _get(f"http://127.0.0.1:{free_port}/", timeout=5.0)

                async with anyio.create_task_group() as tg:

                    async def hold_first():
                        await slow()

                    tg.start_soon(hold_first)

                    # Wait until the first request is in the worker.
                    await to_thread.run_sync(lambda: entered.wait(2.0))

                    # Second request: no worker waiting → 503 immediately.
                    r = await _get(f"http://127.0.0.1:{free_port}/", timeout=2.0)
                    assert r.status_code == 503

                    release.set()
            finally:
                release.set()

            lt.shutdown()
            await lt.stopped

    async def test_rendezvous_idle_worker_dispatches_normally(self, free_port):
        """backlog=0 must not 503 when a worker IS waiting. Sequential
        requests on max_concurrency=1 are the regression check — each
        request finishes before the next arrives, so the worker is always
        idle on get() at dispatch time."""

        def handler(ctx: HTTPReqCtx):
            ctx.complete(Response(status_code=200, headers=[(b"content-length", b"2")]), b"ok")

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with _serve_pooled(cfg, handler, max_concurrency=1) as lt:
            await lt.started
            await _wait_server_ready(free_port)

            for _ in range(5):
                r = await _get(f"http://127.0.0.1:{free_port}/", timeout=2.0)
                assert r.status_code == 200

            lt.shutdown()
            await lt.stopped

    async def test_backlog_admits_extra_requests(self, free_port):
        """backlog=2 with max_concurrency=2: 4 concurrent slow requests all
        succeed (2 in flight + 2 buffered)."""
        entered = threading.Semaphore(0)
        release = threading.Event()

        def body_handler(ctx: HTTPReqCtx):
            entered.release()
            release.wait(timeout=5.0)
            ctx.complete(Response(status_code=200, headers=[(b"content-length", b"2")]), b"ok")

        def handler(_ctx: HTTPReqCtx):
            return body_handler

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with _serve_pooled(cfg, handler, max_concurrency=2, backlog=2) as lt:
            await lt.started
            await _wait_server_ready(free_port)

            results: list[httpx.Response | None] = [None] * 4

            async def fire(i: int) -> None:
                results[i] = await _get(f"http://127.0.0.1:{free_port}/", timeout=5.0)

            try:
                async with anyio.create_task_group() as tg:
                    for i in range(4):
                        tg.start_soon(fire, i)

                    # Two workers run concurrently; the other two sit in the
                    # backlog. Wait for the first wave to enter, then release.
                    def wait_for_two():
                        for _ in range(2):
                            assert entered.acquire(timeout=5.0)

                    await to_thread.run_sync(wait_for_two)
                    release.set()
            finally:
                release.set()

            for r in results:
                assert r is not None
                assert r.status_code == 200

            lt.shutdown()
            await lt.stopped

    async def test_backlog_overflow_rejects_with_503(self, free_port):
        """backlog=1 with max_concurrency=1: 1 worker + 1 buffered = 2
        capacity. A 3rd concurrent request is rejected with 503."""
        entered = threading.Event()
        release = threading.Event()

        def body_handler(ctx: HTTPReqCtx):
            entered.set()
            release.wait(timeout=5.0)
            ctx.complete(Response(status_code=200, headers=[(b"content-length", b"2")]), b"ok")

        def handler(_ctx: HTTPReqCtx):
            return body_handler

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with _serve_pooled(cfg, handler, max_concurrency=1, backlog=1) as lt:
            await lt.started
            await _wait_server_ready(free_port)

            sockets: list[socket.socket] = []

            def open_slow() -> socket.socket:
                sock = socket.create_connection(("127.0.0.1", free_port), timeout=2.0)
                sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
                return sock

            try:
                # Saturate worker.
                sockets.append(await to_thread.run_sync(open_slow))
                assert await to_thread.run_sync(lambda: entered.wait(2.0))
                # Saturate the single backlog slot.
                sockets.append(await to_thread.run_sync(open_slow))
                await anyio.sleep(0.1)

                # 3rd request must 503.
                def probe() -> bytes:
                    s = socket.create_connection(("127.0.0.1", free_port), timeout=2.0)
                    sockets.append(s)
                    s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
                    return read_http_response(s, deadline=2.0)

                response = await to_thread.run_sync(probe)
                assert b"HTTP/1.1 503" in response, response
            finally:
                release.set()
                for s in sockets:
                    with contextlib.suppress(OSError):
                        s.close()

            lt.shutdown()
            await lt.stopped


class TestMultiSelector:
    """``selectors=N > 1`` spawns N independent ``BaseServer`` threads bound
    to the same address via ``SO_REUSEPORT``. Tests assert correctness; the
    kernel-side distribution of incoming connections across selectors is a
    deployment-time concern verified by the bench, not unit tests."""

    async def test_invalid_selectors(self, free_port):
        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        with pytest.raises(ValueError, match="selectors"):
            http_server(cfg, _handler_200(), selectors=0)

    async def test_serves_requests_inline(self, free_port):
        """selectors=4, no thread pool. Each request runs on whichever
        selector accepted it; all must serve correctly."""
        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with serve(http_server(cfg, _handler_200(b"hi"), selectors=4)) as lt:
            await lt.started
            await _wait_server_ready(free_port)

            for _ in range(5):
                resp = await _get(f"http://127.0.0.1:{free_port}/")
                assert resp.status_code == 200
                assert resp.text == "hi"

            lt.shutdown()
            await lt.stopped
        assert lt.exit_code == 0

    async def test_serves_requests_pooled_concurrent(self, free_port):
        """selectors=4 + thread pool. Multiple selectors push onto the
        single shared channel; the pool drains across all producers."""
        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        # backlog=8: with 10 fast requests against max_concurrency=4, headroom keeps the
        # rendezvous race from intermittently 503'ing late arrivals during the test.
        async with thread_pool_handler(_handler_200(b"hi"), max_concurrency=4, backlog=8) as wrapped:
            async with serve(http_server(cfg, wrapped, selectors=4)) as lt:
                await lt.started
                await _wait_server_ready(free_port)

                results: list[httpx.Response | None] = [None] * 10

                async def fire(i: int) -> None:
                    results[i] = await _get(f"http://127.0.0.1:{free_port}/")

                async with anyio.create_task_group() as tg:
                    for i in range(10):
                        tg.start_soon(fire, i)

                for r in results:
                    assert r is not None
                    assert r.status_code == 200
                    assert r.text == "hi"

                lt.shutdown()
                await lt.stopped
        assert lt.exit_code == 0

    async def test_shutdown_stops_all_selectors(self, free_port):
        """All N selector threads must exit on shutdown — no leaked threads
        and a clean exit code."""
        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with serve(http_server(cfg, _handler_200(b"x"), selectors=4)) as lt:
            await lt.started
            await _wait_server_ready(free_port)
            resp = await _get(f"http://127.0.0.1:{free_port}/")
            assert resp.status_code == 200
            lt.shutdown()
            await lt.stopped
        assert lt.exit_code == 0


class TestAcceptorTopology:
    """``acceptor=True`` runs a dedicated acceptor thread that round-robins
    new conns to N worker selectors via the cross-thread op queue."""

    async def test_serves_requests(self, free_port):
        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with serve(http_server(cfg, _handler_200(b"hi"), selectors=4, acceptor=True)) as lt:
            await lt.started
            await _wait_server_ready(free_port)

            for _ in range(8):
                resp = await _get(f"http://127.0.0.1:{free_port}/")
                assert resp.status_code == 200
                assert resp.text == "hi"

            lt.shutdown()
            await lt.stopped
        assert lt.exit_code == 0

    async def test_distributes_conns_across_workers(self, free_port):
        """N concurrent fresh conns should land on multiple worker selectors
        (round-robin). We sample the worker thread id per request — with
        N=4 workers and 8 fresh conns we expect at least 2 distinct workers
        (loose bound to keep this stable under scheduling jitter)."""
        threads_seen: set[int] = set()
        lock = threading.Lock()

        def handler(ctx: HTTPReqCtx) -> BodyHandler | None:
            with lock:
                threads_seen.add(threading.get_ident())
            ctx.complete(
                Response(status_code=200, headers=[(b"content-length", b"2")]),
                b"hi",
            )
            return None

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with serve(http_server(cfg, handler, selectors=4, acceptor=True)) as lt:
            await lt.started
            await _wait_server_ready(free_port)

            # Use fresh connections — keep-alive would pin every request to
            # the same worker. ``httpx.Client(... headers={"Connection":
            # "close"})`` is the simplest way.
            results: list[httpx.Response | None] = [None] * 8

            async def fire(i: int) -> None:
                async with httpx.AsyncClient(timeout=5) as client:
                    results[i] = await client.get(
                        f"http://127.0.0.1:{free_port}/",
                        headers={"Connection": "close"},
                    )

            async with anyio.create_task_group() as tg:
                for i in range(8):
                    tg.start_soon(fire, i)

            for r in results:
                assert r is not None
                assert r.status_code == 200

            assert len(threads_seen) >= 2, f"expected round-robin across workers, got {threads_seen}"

            lt.shutdown()
            await lt.stopped
        assert lt.exit_code == 0

    async def test_shutdown_clean(self, free_port):
        """Acceptor + workers shut down cleanly on lt.shutdown()."""
        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with serve(http_server(cfg, _handler_200(b"x"), selectors=4, acceptor=True)) as lt:
            await lt.started
            await _wait_server_ready(free_port)
            resp = await _get(f"http://127.0.0.1:{free_port}/")
            assert resp.status_code == 200
            lt.shutdown()
            await lt.stopped
        assert lt.exit_code == 0

    async def test_serves_requests_httptools(self, free_port):
        """Acceptor + httptools backend.

        The cross-thread handoff (``_OpTrack`` → ``Selector.post_track``) is
        parser-agnostic at the type level, but httptools' push-callback model
        means the parser is *constructed* on the acceptor thread (inside
        ``RoundRobinAcceptor.__call__``) and *driven* on the worker thread.
        Smoke-tests that the parser instance survives the move.
        """
        cfg = ServerConfig(host="127.0.0.1", port=free_port, backend="httptools")
        async with serve(http_server(cfg, _handler_200(b"hi"), selectors=4, acceptor=True)) as lt:
            await lt.started
            await _wait_server_ready(free_port)

            for _ in range(8):
                resp = await _get(f"http://127.0.0.1:{free_port}/")
                assert resp.status_code == 200
                assert resp.text == "hi"

            lt.shutdown()
            await lt.stopped
        assert lt.exit_code == 0

    async def test_serves_requests_pooled(self, free_port):
        """Acceptor + thread_pool_handler — most production-like cell.

        Pool dispatch routes through ``ctx.conn.selector.stop_tracking(ctx.conn)``;
        in acceptor mode that selector is the **worker**, not the acceptor.
        Catches any regression where ``conn.selector`` is misaligned with the
        actual owning selector.
        """
        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        # backlog=8 keeps the rendezvous race from intermittently 503'ing late
        # arrivals while the pool has spare capacity.
        async with _serve_pooled(
            cfg,
            _handler_200(b"hi"),
            max_concurrency=4,
            backlog=8,
            selectors=4,
            acceptor=True,
        ) as lt:
            await lt.started
            await _wait_server_ready(free_port)

            results: list[httpx.Response | None] = [None] * 10

            async def fire(i: int) -> None:
                results[i] = await _get(f"http://127.0.0.1:{free_port}/")

            async with anyio.create_task_group() as tg:
                for i in range(10):
                    tg.start_soon(fire, i)

            for r in results:
                assert r is not None
                assert r.status_code == 200
                assert r.text == "hi"

            lt.shutdown()
            await lt.stopped
        assert lt.exit_code == 0


class TestSelectorThreadFastPath:
    """When the Router is passed directly to ``http_server`` (no ``thread_pool_handler``
    wrapping), every route — including the 404 / 405 paths — runs on the selector
    thread. No thread pool is involved at all, so 404/405 cost is just a regex
    match plus a static response payload."""

    async def test_router_direct_runs_on_one_thread(self, free_port):
        """Matched routes all share a single thread (the selector). 404 returns
        normally even though there's no worker pool to dispatch it through."""
        threads_seen: set[int] = set()
        lock = threading.Lock()

        routes = Routes()

        @routes.get("/hit")
        def hit(ctx: HTTPReqCtx) -> BodyHandler | None:
            with lock:
                threads_seen.add(threading.get_ident())
            ctx.complete(
                Response(
                    status_code=200,
                    headers=[(b"content-type", b"text/plain"), (b"content-length", b"2")],
                ),
                b"ok",
            )
            return None

        assert hit is not None
        router = routes.build()

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with serve(http_server(cfg, router.as_handler())) as lt:
            await lt.started
            await _wait_server_ready(free_port)

            for _ in range(3):
                r = await _get(f"http://127.0.0.1:{free_port}/hit")
                assert r.status_code == 200

            r = await _get(f"http://127.0.0.1:{free_port}/missing")
            assert r.status_code == 404

            # Every matched request was served from the same thread — the
            # selector. With no ``thread_pool_handler`` in the composition,
            # there are no worker threads to fan out to.
            assert len(threads_seen) == 1, threads_seen

            lt.shutdown()
            await lt.stopped


class _FakeSelector:
    def __init__(self) -> None:
        self.stopped = False

    def stop_tracking(self, conn: _FakeConn) -> None:
        self.stopped = True
        conn.tracked = False


class _FakeConn:
    def __init__(self, sock: socket.socket, selector: _FakeSelector) -> None:
        self.sock = sock
        self.tracked = True
        self.selector = selector


class _DeferringCtx:
    def __init__(self, selector: _FakeSelector, conn: _FakeConn) -> None:
        self.selector = selector
        self.conn = conn
        self.request = Request(method=b"POST", target=b"/upload", path=b"/upload", query_string=b"", headers=[])
        self.body = b""
        self.response_status = None
        self.attrs = {}
        self.deferred: Callable[[], None] | None = None

    def _defer_streaming_dispatch(self, dispatcher: Callable[[HTTPReqCtx], None]) -> None:
        self.deferred = lambda: dispatcher(cast(HTTPReqCtx, self))


class TestServiceRobustness:
    async def test_streaming_pool_defers_dispatch_when_context_requests_it(self):
        """Backends with parser callbacks can delay worker start until parsing unwinds."""
        selector = _FakeSelector()
        sock, peer = socket.socketpair()
        ctx = _DeferringCtx(selector, _FakeConn(sock, selector))
        ran = threading.Event()

        def work(_ctx: HTTPReqCtx) -> None:
            ran.set()

        try:
            async with streaming_pool_handler(work, max_concurrency=1) as wrapped:
                assert wrapped(cast(HTTPReqCtx, ctx)) is None
                assert ctx.deferred is not None
                assert selector.stopped is False
                assert ran.wait(0.05) is False

                ctx.deferred()
                assert await to_thread.run_sync(lambda: ran.wait(2.0))
                assert selector.stopped is True
                assert ctx.conn.tracked is False
        finally:
            sock.close()
            peer.close()

    async def test_max_concurrency_caps_parallelism(self, free_port):
        """N+1 requests against max_concurrency=N: peak in-flight is exactly N."""
        in_flight = 0
        peak = 0
        lock = threading.Lock()
        gate = threading.Event()

        def body_handler(ctx: HTTPReqCtx):
            nonlocal in_flight, peak
            with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            gate.wait(timeout=5.0)
            with lock:
                in_flight -= 1
            ctx.complete(Response(status_code=200, headers=[(b"content-length", b"2")]), b"ok")

        def handler(_ctx: HTTPReqCtx):
            return body_handler

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with _serve_pooled(cfg, handler, max_concurrency=3, backlog=2) as lt:
            await lt.started
            await _wait_server_ready(free_port)

            async with anyio.create_task_group() as tg:
                for _ in range(5):
                    tg.start_soon(_get, f"http://127.0.0.1:{free_port}/")

                # Once 3 handlers are in flight, the cap is observable. Wait for it
                # before releasing the gate so we don't race the "still ramping up" state.
                async def wait_for_peak() -> None:
                    while True:
                        with lock:
                            if peak >= 3:
                                return
                        await anyio.sleep(0.02)

                with anyio.fail_after(5.0):
                    await wait_for_peak()
                gate.set()

            assert peak == 3, f"expected peak 3, got {peak}"

            lt.shutdown()
            await lt.stopped

    async def test_pool_overload_rejects_without_blocking_selector(self, free_port):
        """A full worker pool + full queue returns 503 instead of blocking the selector."""
        entered = threading.Event()
        release = threading.Event()

        def body_handler(ctx: HTTPReqCtx):
            entered.set()
            release.wait(timeout=5.0)
            ctx.complete(Response(status_code=200, headers=[(b"content-length", b"2")]), b"ok")

        routes = Routes()

        @routes.get("/slow")
        def slow(_ctx: HTTPReqCtx) -> BodyHandler:
            return body_handler

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with _serve_pooled(cfg, routes.build().as_handler(), max_concurrency=1, backlog=1) as lt:
            await lt.started
            await _wait_server_ready(free_port)

            sockets: list[socket.socket] = []

            def open_slow() -> socket.socket:
                sock = socket.create_connection(("127.0.0.1", free_port), timeout=2.0)
                sock.sendall(b"GET /slow HTTP/1.1\r\nHost: x\r\n\r\n")
                return sock

            def open_slow_and_try_read() -> tuple[socket.socket, bytes]:
                sock = open_slow()
                try:
                    return sock, read_http_response(sock, deadline=0.5)
                except TimeoutError:
                    return sock, b""

            try:
                sockets.append(await to_thread.run_sync(open_slow))
                assert await to_thread.run_sync(lambda: entered.wait(2.0))

                # This request occupies the single pending queue slot while
                # the first request keeps the only worker busy.
                sockets.append(await to_thread.run_sync(open_slow))
                await anyio.sleep(0.1)

                overload_response = b""
                for _ in range(4):
                    sock, response = await to_thread.run_sync(open_slow_and_try_read)
                    sockets.append(sock)
                    if b"HTTP/1.1 503" in response:
                        overload_response = response
                        break

                assert b"HTTP/1.1 503" in overload_response, overload_response
                assert b"Service Unavailable" in overload_response

                # The selector must still be responsive while the worker and
                # pending queue are saturated. A miss is answered inline.
                r = await _get(f"http://127.0.0.1:{free_port}/missing", timeout=1.0)
                assert r.status_code == 404
            finally:
                release.set()
                for sock in sockets:
                    with contextlib.suppress(OSError):
                        sock.close()

            lt.shutdown()
            await lt.stopped

    async def test_handler_exception_returns_500_and_service_stays_up(self, free_port):
        """A handler exception is caught at the connection level and returned as 500.

        The service must remain healthy and serve subsequent requests.
        """

        def boom(_: HTTPReqCtx) -> None:
            raise RuntimeError("handler crashed")

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with _serve_pooled(cfg, boom, max_concurrency=2) as lt:
            await lt.started
            await _wait_server_ready(free_port)

            r1 = await _get(f"http://127.0.0.1:{free_port}/", timeout=2.0)
            assert r1.status_code == 500
            r2 = await _get(f"http://127.0.0.1:{free_port}/", timeout=2.0)
            assert r2.status_code == 500  # service still serving

            lt.shutdown()
            await lt.stopped

        assert lt.exit_code == 0

    async def test_slot_released_after_normal_request(self, free_port):
        """Repeatedly hitting a 1-slot pool must keep working.

        Indirectly confirms the worker channel slot is released on the success
        path — if it weren't, the second request would block forever.
        """

        def handler(ctx: HTTPReqCtx):
            ctx.complete(Response(status_code=200, headers=[(b"content-length", b"2")]), b"ok")

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with _serve_pooled(cfg, handler, max_concurrency=1) as lt:
            await lt.started
            await _wait_server_ready(free_port)

            for _ in range(5):
                resp = await _get(f"http://127.0.0.1:{free_port}/")
                assert resp.status_code == 200

            lt.shutdown()
            await lt.stopped


class TestDispatchLoad:
    async def test_many_requests_served_from_worker_threads(self, free_port):
        cfg = ServerConfig(host="127.0.0.1", port=free_port)

        def body_handler(ctx: HTTPReqCtx):
            tid = str(threading.get_ident()).encode()
            ctx.complete(
                Response(status_code=200, headers=[(b"content-length", str(len(tid)).encode())]),
                tid,
            )

        def handler(_ctx: HTTPReqCtx):
            return body_handler

        # backlog gives 10 fast requests headroom past the rendezvous default,
        # so this test isolates the dispatch-distribution check from admission timing.
        async with _serve_pooled(cfg, handler, max_concurrency=8, backlog=4) as lt:
            await lt.started
            await _wait_server_ready(free_port)

            results: list[str] = []

            async def fire():
                r = await _get(f"http://127.0.0.1:{free_port}/")
                results.append(r.text)

            async with anyio.create_task_group() as tg:
                for _ in range(10):
                    tg.start_soon(fire)

            # Some distribution across worker threads — at least 2 distinct thread names.
            counts = Counter(results)
            assert sum(counts.values()) == 10
            assert len(counts) >= 2


class TestRequestCancellation:
    async def test_check_cancelled_outside_handler_raises_lookup_error(self):
        with pytest.raises(LookupError, match="outside a request handler"):
            check_cancelled()

    async def test_client_disconnect_cancels_handler(self, free_port):
        """A handler doing slow work sees ``RequestCancelled`` when the client closes the socket.

        The watchdog only arms for requests without a body, which is the case for the
        bare ``GET /`` we open here. We close the socket before reading the response so
        the server detects EOF mid-handler.
        """
        handler_started = threading.Event()
        handler_cancelled = threading.Event()

        def body_handler(ctx: HTTPReqCtx):
            handler_started.set()
            try:
                for _ in range(200):
                    check_cancelled()
                    time.sleep(0.02)
            except RequestCancelled:
                handler_cancelled.set()
                raise
            # Should not be reached
            ctx.complete(Response(status_code=200, headers=[(b"content-length", b"2")]), b"ok")

        def handler(_ctx: HTTPReqCtx):
            return body_handler

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with _serve_pooled(cfg, handler, max_concurrency=2) as lt:
            await lt.started
            await _wait_server_ready(free_port)

            def hit_and_drop():
                # Open a raw socket, send a minimal GET, then close mid-handler.
                s = socket.create_connection(("127.0.0.1", free_port), timeout=2.0)
                s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
                handler_started.wait(2.0)
                s.close()  # peer FIN — selector watchdog should fire

            await to_thread.run_sync(hit_and_drop)

            # Wait for cancellation to propagate (selector poll interval + check_cancelled poll interval).
            def wait_for_cancel():
                return handler_cancelled.wait(5.0)

            assert await to_thread.run_sync(wait_for_cancel)

            lt.shutdown()
            await lt.stopped
