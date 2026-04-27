"""Tests for localpost.http._service (the hosted http_server service)."""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from collections import Counter

import anyio
import h11
import httpx
import pytest
from anyio import to_thread

from localpost.hosting import serve
from localpost.http import (
    HTTPReqCtx,
    RequestCancelled,
    RequestCtx,
    Response,
    Routes,
    ServerConfig,
    check_cancelled,
    http_server,
)

pytestmark = pytest.mark.anyio


def _handler_200(body: bytes = b"ok"):
    def handler(ctx: HTTPReqCtx):
        ctx.complete(
            h11.Response(
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
    async def test_serves_single_request(self, free_port):
        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        svc = http_server(cfg, _handler_200(b"hi"))
        async with serve(svc) as lt:
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

        def handler(ctx: HTTPReqCtx):
            with lock:
                thread_ids.append(threading.get_ident())
            entered.release()
            # Block until the test releases us; this forces parallelism.
            release.wait(timeout=5.0)
            ctx.complete(
                h11.Response(status_code=200, headers=[(b"content-length", b"2")]),
                b"ok",
            )

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        svc = http_server(cfg, handler, max_concurrency=4)
        async with serve(svc) as lt:
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
        """With max_concurrency=1, requests are handled one at a time."""
        in_flight = 0
        peak = 0
        lock = threading.Lock()

        def handler(ctx: HTTPReqCtx):
            nonlocal in_flight, peak
            with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            time.sleep(0.1)
            with lock:
                in_flight -= 1
            ctx.complete(h11.Response(status_code=200, headers=[(b"content-length", b"2")]), b"ok")

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        svc = http_server(cfg, handler, max_concurrency=1)
        async with serve(svc) as lt:
            await lt.started
            await _wait_server_ready(free_port)

            async with anyio.create_task_group() as tg:
                for _ in range(3):
                    tg.start_soon(_get, f"http://127.0.0.1:{free_port}/")

            assert peak == 1

            lt.shutdown()
            await lt.stopped

    async def test_shutdown_cancels_inflight(self, free_port):
        """Triggering shutdown while a handler is running cancels it via the HTTP cancellation token."""
        handler_started = threading.Event()
        handler_cancelled = threading.Event()

        def handler(ctx: HTTPReqCtx):
            handler_started.set()
            # Cooperate with cancellation via localpost.http.check_cancelled
            try:
                for _ in range(100):
                    check_cancelled()
                    time.sleep(0.05)
            except RequestCancelled:
                handler_cancelled.set()
                raise
            ctx.complete(h11.Response(status_code=200, headers=[(b"content-length", b"2")]), b"ok")

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        svc = http_server(cfg, handler, max_concurrency=2)
        async with serve(svc) as lt:
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

        # The handler's own loop observes cancellation via from_thread.check_cancelled.
        assert handler_cancelled.is_set()

    async def test_router_dispatch_via_service(self, free_port):
        routes = Routes()

        @routes.get("/books/{id}")
        def get_book(ctx: RequestCtx) -> Response:
            return Response(200, {"content-type": "text/plain"}, [f"book={ctx.path_args['id']}".encode()])

        assert get_book is not None
        router = routes.build()

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        svc = http_server(cfg, router.as_handler(), max_concurrency=4)
        async with serve(svc) as lt:
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
            http_server(ServerConfig(), _handler_200(), max_concurrency=0)


class TestServiceRobustness:
    async def test_max_concurrency_caps_parallelism(self, free_port):
        """N+1 requests against max_concurrency=N: peak in-flight is exactly N."""
        in_flight = 0
        peak = 0
        lock = threading.Lock()
        gate = threading.Event()

        def handler(ctx: HTTPReqCtx):
            nonlocal in_flight, peak
            with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            gate.wait(timeout=5.0)
            with lock:
                in_flight -= 1
            ctx.complete(h11.Response(status_code=200, headers=[(b"content-length", b"2")]), b"ok")

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        svc = http_server(cfg, handler, max_concurrency=3)
        async with serve(svc) as lt:
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

    async def test_handler_exception_returns_500_and_service_stays_up(self, free_port):
        """A handler exception is caught at the connection level and returned as 500.

        The service must remain healthy and serve subsequent requests.
        """

        def boom(_: HTTPReqCtx) -> None:
            raise RuntimeError("handler crashed")

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        svc = http_server(cfg, boom, max_concurrency=2)
        async with serve(svc) as lt:
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
        """Repeatedly hitting a max_concurrency=1 service must keep working.

        Indirectly confirms ``req_slots.release()`` runs on the success path —
        if it didn't, the second request would block forever on the semaphore.
        """

        def handler(ctx: HTTPReqCtx):
            ctx.complete(h11.Response(status_code=200, headers=[(b"content-length", b"2")]), b"ok")

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        svc = http_server(cfg, handler, max_concurrency=1)
        async with serve(svc) as lt:
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

        def handler(ctx: HTTPReqCtx):
            tid = str(threading.get_ident()).encode()
            ctx.complete(
                h11.Response(status_code=200, headers=[(b"content-length", str(len(tid)).encode())]),
                tid,
            )

        svc = http_server(cfg, handler, max_concurrency=8)
        async with serve(svc) as lt:
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

        def handler(ctx: HTTPReqCtx):
            handler_started.set()
            try:
                for _ in range(200):
                    check_cancelled()
                    time.sleep(0.02)
            except RequestCancelled:
                handler_cancelled.set()
                raise
            # Should not be reached
            ctx.complete(h11.Response(status_code=200, headers=[(b"content-length", b"2")]), b"ok")

        cfg = ServerConfig(host="127.0.0.1", port=free_port)
        svc = http_server(cfg, handler, max_concurrency=2)
        async with serve(svc) as lt:
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

            lt.shutdown()
            await lt.stopped
