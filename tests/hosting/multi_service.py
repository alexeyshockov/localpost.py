"""Tests for multi-service composition (run_app(*svcs) → _run_many).

The composed parent service started by ``_run_many`` doesn't call
``set_started`` itself — it just spawns each child via ``lt.start`` and waits
for them to stop. So the natural shape for these tests is to give each child
a finite task and observe that the composition reaches ``stopped`` only after
every child has.
"""

from __future__ import annotations

import anyio
import pytest

from localpost.hosting import ServiceLifetime, run
from localpost.hosting._host import _run_many

pytestmark = pytest.mark.anyio


class TestRunMany:
    async def test_runs_all_children_to_completion(self):
        """All children start and complete; the composition stops cleanly."""
        events: list[str] = []
        events_lock = anyio.Lock()

        async def make_finite_child(label: str, sleep: float):
            async def svc(lt: ServiceLifetime) -> None:
                async with events_lock:
                    events.append(f"{label}:start")
                lt.set_started()
                await anyio.sleep(sleep)
                async with events_lock:
                    events.append(f"{label}:stop")

            return svc

        a = await make_finite_child("a", 0.05)
        b = await make_finite_child("b", 0.10)
        c = await make_finite_child("c", 0.02)

        composed = _run_many(a, b, c)
        exit_code = await run(composed)

        assert exit_code == 0
        # All children have started and stopped.
        assert {"a:start", "b:start", "c:start", "a:stop", "b:stop", "c:stop"} <= set(events)


class TestRunManyChildCrashSiblingsKeepRunningToday:
    """Pin the documented limitation of ``_run_many``: when one child crashes,
    siblings keep running until they finish on their own.

    See the docstring on ``_run_many``. When the behavior is hardened (so a
    crashed child cancels its siblings), this test should flip — assert that
    ``b`` was cancelled instead of allowed to finish.
    """

    async def test_sibling_runs_to_completion_when_other_child_crashes(self):
        b_finished_naturally = anyio.Event()
        a_crashed = anyio.Event()

        async def a(lt: ServiceLifetime) -> None:
            lt.set_started()
            a_crashed.set()
            raise RuntimeError("a crashed")

        async def b(lt: ServiceLifetime) -> None:
            lt.set_started()
            await anyio.sleep(0.2)  # finite; long enough to outlive a's crash
            b_finished_naturally.set()

        composed = _run_many(a, b)
        exit_code = await run(composed)

        # `a` crashed (errors absorbed at the lifetime level → exit_code stays 0
        # for the composition since the parent's _run isn't the one raising;
        # the child's own exit_code is 1, but _run_many doesn't propagate that).
        assert a_crashed.is_set()
        # Today: b ran to natural completion despite a's crash.
        assert b_finished_naturally.is_set(), (
            "If this fails, _run_many learned to cancel siblings on child error — flip the assertion."
        )
        # exit_code reflects the composed parent, which didn't itself raise.
        assert exit_code == 0
