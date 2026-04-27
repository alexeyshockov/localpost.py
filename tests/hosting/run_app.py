"""Tests for ``localpost.hosting.run_app`` — the documented sync entrypoint.

``run_app`` is the headline API users call from ``__main__``. It composes
``shutdown_on_signal`` middleware with the supplied services and drives them
under ``anyio.run`` with the platform-default backend.

These tests run synchronously (not under pytest-anyio) because ``run_app``
itself takes care of starting an event loop.
"""

from __future__ import annotations

import threading

import anyio

from localpost.hosting import ServiceLifetime, run_app


def test_run_app_with_single_service_returns_zero():
    """A finite single service runs to completion and ``run_app`` returns 0."""

    async def finite_svc(lt: ServiceLifetime) -> None:
        lt.set_started()
        await anyio.sleep(0.05)

    assert run_app(finite_svc) == 0


def test_run_app_with_failing_service_returns_nonzero():
    """If the service raises, exit_code is 1."""

    async def boom(lt: ServiceLifetime) -> None:
        lt.set_started()
        raise RuntimeError("boom")

    assert run_app(boom) == 1


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

    assert run_app(make_svc("a"), make_svc("b"), make_svc("c")) == 0
    assert set(finished) == {"a", "b", "c"}
