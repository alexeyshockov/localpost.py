"""Tests for the smaller pieces of the hosting public surface that the
existing host.py / hosted_service.py tests don't cover: contextvar accessors,
defer/adefer, observe_services, cancel_on_shutdown / cancel_on_stop.
"""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager

import anyio
import pytest

from localpost.hosting import (
    ServiceLifetime,
    ServiceLifetimeView,
    observe_services,
    serve,
)
from localpost.hosting._host import current_app, current_service

pytestmark = pytest.mark.anyio


# --- current_service / current_app -------------------------------------------


class TestCurrentLifetimeAccessors:
    async def test_current_service_outside_hosting_raises(self):
        with pytest.raises(RuntimeError, match="Not in hosting context"):
            current_service()

    async def test_current_app_outside_hosting_raises(self):
        with pytest.raises(RuntimeError, match="Not in hosting context"):
            current_app()

    async def test_current_service_inside_hosting_returns_view(self):
        seen: dict = {}

        async def svc(lt: ServiceLifetime) -> None:
            seen["view"] = current_service()
            seen["is_view"] = isinstance(seen["view"], ServiceLifetimeView)
            lt.set_started()

        async with serve(svc) as lt:
            await lt.stopped

        assert seen["is_view"] is True

    async def test_current_app_inside_hosting_returns_root(self):
        """``current_app`` in a child service points at the root, not the child."""
        seen: dict = {}

        async def child(lt: ServiceLifetime) -> None:
            seen["child"] = current_service()
            seen["app"] = current_app()
            lt.set_started()
            await lt.shutting_down.wait()

        async def parent(lt: ServiceLifetime) -> None:
            child_lt = lt.start(child)
            await child_lt.started
            lt.set_started()
            await lt.shutting_down.wait()
            child_lt.shutdown()
            await child_lt.stopped

        async with serve(parent) as lt:
            await lt.started
            lt.shutdown()
            await lt.stopped

        # In the child, current_service was the child; current_app was the root.
        assert seen["child"] is not seen["app"]


# --- defer / adefer ----------------------------------------------------------


class TestDefer:
    async def test_defer_releases_sync_cm_on_stop(self):
        events: list[str] = []

        @contextmanager
        def resource():
            events.append("acquire")
            try:
                yield "value"
            finally:
                events.append("release")

        async def svc(lt: ServiceLifetime) -> None:
            value = lt.defer(resource())
            assert value == "value"
            events.append(f"running:{value}")
            lt.set_started()
            await lt.shutting_down.wait()

        async with serve(svc) as lt:
            await lt.started
            assert events == ["acquire", "running:value"]
            lt.shutdown()
            await lt.stopped

        assert events == ["acquire", "running:value", "release"]

    async def test_defer_releases_closable(self):
        closed: list[bool] = [False]

        class Closable:
            def close(self) -> None:
                closed[0] = True

        async def svc(lt: ServiceLifetime) -> None:
            lt.defer(Closable())
            lt.set_started()
            await lt.shutting_down.wait()

        async with serve(svc) as lt:
            await lt.started
            lt.shutdown()
            await lt.stopped

        assert closed[0] is True

    async def test_adefer_releases_async_cm_on_stop(self):
        events: list[str] = []

        @asynccontextmanager
        async def resource():
            events.append("acquire")
            try:
                yield "value"
            finally:
                events.append("release")

        async def svc(lt: ServiceLifetime) -> None:
            value = await lt.adefer(resource())
            assert value == "value"
            events.append(f"running:{value}")
            lt.set_started()
            await lt.shutting_down.wait()

        async with serve(svc) as lt:
            await lt.started
            assert events == ["acquire", "running:value"]
            lt.shutdown()
            await lt.stopped

        assert events == ["acquire", "running:value", "release"]


# --- observe_services --------------------------------------------------------


class TestObserveServices:
    async def test_waits_for_all_started_then_shuts_down_on_exit(self):
        timeline: list[str] = []

        async def make_svc(label: str):
            async def svc(lt: ServiceLifetime) -> None:
                timeline.append(f"{label}:starting")
                lt.set_started()
                timeline.append(f"{label}:running")
                await lt.shutting_down.wait()
                timeline.append(f"{label}:shutting-down")

            return svc

        a_svc, b_svc = await make_svc("a"), await make_svc("b")

        async with serve(a_svc) as a_lt, serve(b_svc) as b_lt:
            async with observe_services(a_lt, b_lt):
                # Both must be running by the time observe_services yields.
                assert "a:running" in timeline
                assert "b:running" in timeline

            # On exit both are shut down.
            await a_lt.stopped
            await b_lt.stopped

        assert "a:shutting-down" in timeline
        assert "b:shutting-down" in timeline


# --- cancel_on_shutdown / cancel_on_stop -------------------------------------


class TestCancelOnLifetimeEvent:
    async def test_cancel_on_shutdown_unblocks_when_service_shuts_down(self):
        completed = anyio.Event()

        async def svc(lt: ServiceLifetime) -> None:
            async def long_task() -> None:
                try:
                    await anyio.sleep(60)
                finally:
                    completed.set()

            lt.tg.start_soon(lt.view.cancel_on_shutdown(long_task))
            lt.set_started()
            await lt.shutting_down.wait()

        async with serve(svc) as lt:
            await lt.started
            lt.shutdown()
            await lt.stopped

        # cancel_on_shutdown wraps the task so its outer scope cancels when
        # the service starts shutting down.
        assert completed.is_set()

    async def test_cancel_on_stop_unblocks_after_full_stop(self):
        completed = anyio.Event()

        async def child(lt: ServiceLifetime) -> None:
            async def long_task() -> None:
                try:
                    await anyio.sleep(60)
                finally:
                    completed.set()

            lt.tg.start_soon(lt.view.cancel_on_stop(long_task))
            lt.set_started()
            await lt.shutting_down.wait()

        async with serve(child) as lt:
            await lt.started
            lt.shutdown()
            await lt.stopped

        assert completed.is_set()
