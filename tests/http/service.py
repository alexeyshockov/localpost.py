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

pytestmark = pytest.mark.anyio


@asynccontextmanager
async def _serve_pooled(
    cfg: ServerConfig,
    handler: RequestHandler,
    *,
    selectors: int = 1,
    acceptor: bool = False,
) -> AsyncGenerator[ServiceLifetimeView]:
    """Compose ``thread_pool_handler`` + ``http_server`` and yield the http_server lifetime view.

    Tests own shutdown via the yielded lifetime; the pool drains on exit.
    """
    async with thread_pool_handler(handler) as wrapped:
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
        async with _serve_pooled(cfg, _handler_200(b"hi")) as lt:
            await lt.started
            await _wait_server_ready(free_port)
            resp = await _get(f"http://127.0.0.1:{free_port}/")
            assert resp.status_code == 200
            assert resp.text == "hi"
            lt.shutdown()
            await lt.stopped
        assert lt.exit_code == 0

    async def test_each_request_becomes_a_task(self, free_port):
        """Several slow handlers run in parallel on different worker threads."""
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
        async with _serve_pooled(cfg, handler) as lt:
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
        async with _serve_pooled(cfg, handler) as lt:
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
        async with _serve_pooled(cfg, router.as_handler()) as lt:
            await lt.started
            await _wait_server_ready(free_port)

            resp = await _get(f"http://127.0.0.1:{free_port}/books/42")
            assert resp.status_code == 200
            assert resp.text == "book=42"

            resp = await _get(f"http://127.0.0.1:{free_port}/missing")
            assert resp.status_code == 404

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
        async with thread_pool_handler(_handler_200(b"hi")) as wrapped:
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
        async with _serve_pooled(
            cfg,
            _handler_200(b"hi"),
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
            async with streaming_pool_handler(work) as wrapped:
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

    async def test_handler_exception_returns_500_and_service_stays_up(self, free_port):
        """A handler exception is caught at the connection level and returned as 500.

        The service must remain healthy and serve subsequent requests.
        """

        def boom(_: HTTPReqCtx) -> None:
            raise RuntimeError("handler crashed")

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with _serve_pooled(cfg, boom) as lt:
            await lt.started
            await _wait_server_ready(free_port)

            r1 = await _get(f"http://127.0.0.1:{free_port}/", timeout=2.0)
            assert r1.status_code == 500
            r2 = await _get(f"http://127.0.0.1:{free_port}/", timeout=2.0)
            assert r2.status_code == 500  # service still serving

            lt.shutdown()
            await lt.stopped

        assert lt.exit_code == 0

    async def test_repeated_requests_keep_working(self, free_port):
        """Repeatedly hitting the pool must keep working — confirms workers
        re-park (back into the shared idle deque) after each task."""

        def handler(ctx: HTTPReqCtx):
            ctx.complete(Response(status_code=200, headers=[(b"content-length", b"2")]), b"ok")

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        async with _serve_pooled(cfg, handler) as lt:
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

        async with _serve_pooled(cfg, handler) as lt:
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
        async with _serve_pooled(cfg, handler) as lt:
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
