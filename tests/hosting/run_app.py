"""Tests for ``localpost.hosting.run_app`` — the documented sync entrypoint.

``run_app`` is the headline API users call from ``__main__``. It composes
``shutdown_on_signal`` middleware with the supplied services, drives them
under ``anyio.run`` with the platform-default backend, and exits the process
via ``SystemExit`` with the resulting status code.

These tests run synchronously (not under pytest-anyio) because ``run_app``
itself takes care of starting an event loop.
"""

from __future__ import annotations

import threading

import anyio
import pytest

from localpost.hosting import ServiceLifetime, run_app


def test_run_app_with_single_service_exits_zero():
    """A finite single service runs to completion and ``run_app`` exits 0."""

    async def finite_svc(lt: ServiceLifetime) -> None:
        lt.set_started()
        await anyio.sleep(0.05)

    with pytest.raises(SystemExit) as exc_info:
        run_app(finite_svc)
    assert exc_info.value.code == 0


def test_run_app_with_failing_service_exits_nonzero():
    """If the service raises, the process exits with code 1."""

    async def boom(lt: ServiceLifetime) -> None:
        lt.set_started()
        raise RuntimeError("boom")

    with pytest.raises(SystemExit) as exc_info:
        run_app(boom)
    assert exc_info.value.code == 1


def test_run_app_with_multiple_services_runs_all():
    """``run_app(*svcs)`` composes via _run_many and runs every service."""
    finished: list[str] = []
    lock = threading.Lock()

    def make_svc(label: str):
        async def svc(lt: ServiceLifetime) -> None:
            lt.set_started()
            await anyio.sleep(0.02)
            with lock:
                finished.append(label)

        return svc

    with pytest.raises(SystemExit) as exc_info:
        run_app(make_svc("a"), make_svc("b"), make_svc("c"))
    assert exc_info.value.code == 0
    assert set(finished) == {"a", "b", "c"}
