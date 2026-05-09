"""Tests for ``localpost.threadtools.TaskGroup``.

These run against a per-test :class:`WorkerExecutor` provided by
``conftest.py``. The TaskGroup itself is sync; the executor is sync.
No portal / async context is needed.
"""

from __future__ import annotations

import contextvars
import threading
import time
from concurrent.futures import Future

import pytest

from localpost.threadtools import TaskGroup, WorkerExecutor

# ---------------------------------------------------------------------------
# Basic submit / result
# ---------------------------------------------------------------------------


def test_start_soon_returns_none(executor: WorkerExecutor):
    with TaskGroup(executor) as tg:
        result = tg.start_soon(lambda: 42)
    assert result is None


def test_create_task_returns_future_with_result(executor: WorkerExecutor):
    with TaskGroup(executor) as tg:
        fut = tg.create_task(lambda x: x * 2, 21)
        assert isinstance(fut, Future)
        assert fut.result(timeout=5) == 42


def test_create_task_with_kwargs(executor: WorkerExecutor):
    with TaskGroup(executor) as tg:
        fut = tg.create_task(lambda *, a, b: a + b, a=1, b=2)
        assert fut.result(timeout=5) == 3


def test_many_concurrent_tasks_all_complete(executor: WorkerExecutor):
    n = 50

    def work(i: int) -> int:
        time.sleep(0.005)
        return i * i

    with TaskGroup(executor) as tg:
        futs = [tg.create_task(work, i) for i in range(n)]

    assert sorted(f.result() for f in futs) == [i * i for i in range(n)]


# ---------------------------------------------------------------------------
# Construction outside an entered context
# ---------------------------------------------------------------------------


def test_create_task_before_enter_raises(executor: WorkerExecutor):
    tg = TaskGroup(executor)
    with pytest.raises(RuntimeError, match="entered"):
        tg.create_task(lambda: None)


# ---------------------------------------------------------------------------
# Exception capture
# ---------------------------------------------------------------------------


def test_create_task_exception_captured_in_future_and_raised_at_exit(executor: WorkerExecutor):
    class Boom(Exception):
        pass

    def bad():
        raise Boom("nope")

    with pytest.raises(ExceptionGroup) as ei:  # noqa: PT012
        with TaskGroup(executor) as tg:
            fut = tg.create_task(bad)
            with pytest.raises(Boom):
                fut.result(timeout=5)
    assert len(ei.value.exceptions) == 1
    assert isinstance(ei.value.exceptions[0], Boom)


def test_start_soon_exception_surfaces_in_group(executor: WorkerExecutor):
    """Even fire-and-forget tasks have their errors escalate to the group."""

    class Boom(Exception):
        pass

    def bad():
        raise Boom("nope")

    with pytest.raises(ExceptionGroup) as ei:
        with TaskGroup(executor) as tg:
            tg.start_soon(bad)

    assert len(ei.value.exceptions) == 1
    assert isinstance(ei.value.exceptions[0], Boom)


def test_create_task_result_reraise_is_deduped_in_group(executor: WorkerExecutor):
    """When ``Future.result()`` re-raises a task exception into the body,
    ``__exit__`` collapses the body copy with the group-recorded copy
    (same instance) — surfacing the failure exactly once."""

    class Boom(Exception):
        pass

    def bad():
        raise Boom("nope")

    with pytest.raises(ExceptionGroup) as ei:
        with TaskGroup(executor) as tg:
            tg.create_task(bad).result(timeout=5)

    assert len(ei.value.exceptions) == 1
    assert isinstance(ei.value.exceptions[0], Boom)


def test_distinct_body_and_task_exceptions_are_both_surfaced(executor: WorkerExecutor):
    """Dedup is by identity — two different exception instances must both
    appear in the ExceptionGroup, even if they're the same type."""

    class Boom(Exception):
        pass

    def bad():
        raise Boom("from-task")

    with pytest.raises(ExceptionGroup) as ei:  # noqa: PT012
        with TaskGroup(executor) as tg:
            tg.start_soon(bad)
            time.sleep(0.05)
            raise Boom("from-body")

    assert len(ei.value.exceptions) == 2
    messages = sorted(str(e) for e in ei.value.exceptions)
    assert messages == ["from-body", "from-task"]


def test_body_exception_alone_is_wrapped(executor: WorkerExecutor):
    class BodyBoom(Exception):
        pass

    with pytest.raises(ExceptionGroup) as ei:
        with TaskGroup(executor):
            raise BodyBoom("body")
    assert len(ei.value.exceptions) == 1
    assert isinstance(ei.value.exceptions[0], BodyBoom)


def test_body_and_task_exceptions_are_merged(executor: WorkerExecutor):
    class Body(Exception):
        pass

    class Task(Exception):
        pass

    def bad():
        raise Task("task")

    with pytest.raises(ExceptionGroup) as ei:  # noqa: PT012
        with TaskGroup(executor) as tg:
            tg.start_soon(bad)
            time.sleep(0.05)
            raise Body("body")
    types = {type(e) for e in ei.value.exceptions}
    assert types == {Body, Task}


def test_named_group_uses_name_in_exception_message(executor: WorkerExecutor):
    def bad():
        raise RuntimeError("x")

    with pytest.raises(ExceptionGroup, match="TaskGroup 'pool-x' failed"):
        with TaskGroup(executor, name="pool-x") as tg:
            tg.start_soon(bad)


# ---------------------------------------------------------------------------
# Drain semantics
# ---------------------------------------------------------------------------


def test_exit_blocks_until_all_tasks_complete(executor: WorkerExecutor):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def slow():
        started.set()
        release.wait(timeout=5)
        finished.set()

    with TaskGroup(executor) as tg:
        tg.start_soon(slow)
        assert started.wait(timeout=2)
        assert not finished.is_set()
        release.set()
    assert finished.is_set()


def test_nested_create_task_during_drain(executor: WorkerExecutor):
    """A task can spawn more tasks; drain waits for the whole transitive set."""
    counter = 0
    counter_lock = threading.Lock()

    def leaf():
        nonlocal counter
        with counter_lock:
            counter += 1

    def branch(tg: TaskGroup, depth: int):
        if depth == 0:
            leaf()
            return
        f1 = tg.create_task(branch, tg, depth - 1)
        f2 = tg.create_task(branch, tg, depth - 1)
        f1.result(timeout=5)
        f2.result(timeout=5)
        leaf()

    with TaskGroup(executor) as tg:
        tg.start_soon(branch, tg, 3)

    # depth=3 → 1 + 2 + 4 + 8 = 15 leaves
    assert counter == 15


def test_start_soon_after_close_raises(executor: WorkerExecutor):
    tg = TaskGroup(executor)
    with tg:
        tg.create_task(lambda: None).result(timeout=5)
    with pytest.raises(RuntimeError, match="closed"):
        tg.start_soon(lambda: None)


def test_group_cannot_be_reused(executor: WorkerExecutor):
    tg = TaskGroup(executor)
    with tg:
        pass
    with pytest.raises(RuntimeError, match="cannot be reused"):
        with tg:
            pass


# ---------------------------------------------------------------------------
# Cross-thread submission / stress
# ---------------------------------------------------------------------------


def test_start_soon_from_arbitrary_thread(executor: WorkerExecutor):
    """``start_soon`` must be safe to call from any thread."""
    results: list[int] = []
    results_lock = threading.Lock()

    def producer(tg: TaskGroup):
        for i in range(10):
            tg.start_soon(_record, i, results, results_lock)

    with TaskGroup(executor) as tg:
        threads = [threading.Thread(target=producer, args=(tg,)) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert sorted(results) == sorted(list(range(10)) * 5)


def _record(i: int, sink: list[int], lock: threading.Lock) -> None:
    with lock:
        sink.append(i)


# ---------------------------------------------------------------------------
# ContextVar propagation (delegated to the executor)
# ---------------------------------------------------------------------------


def test_task_sees_caller_context(executor: WorkerExecutor):
    var: contextvars.ContextVar[str] = contextvars.ContextVar("test_var")
    var.set("caller-value")

    with TaskGroup(executor) as tg:
        fut = tg.create_task(var.get)

    assert fut.result(timeout=5) == "caller-value"


def test_task_mutation_does_not_leak_to_caller(executor: WorkerExecutor):
    var: contextvars.ContextVar[str] = contextvars.ContextVar("test_var", default="original")

    def mutate():
        var.set("task-mutated")
        return var.get()

    with TaskGroup(executor) as tg:
        fut = tg.create_task(mutate)

    assert fut.result(timeout=5) == "task-mutated"
    assert var.get() == "original"


def test_each_create_task_captures_independently(executor: WorkerExecutor):
    var: contextvars.ContextVar[int] = contextvars.ContextVar("test_var", default=0)

    with TaskGroup(executor) as tg:
        var.set(1)
        f1 = tg.create_task(var.get)
        var.set(2)
        f2 = tg.create_task(var.get)

    assert f1.result(timeout=5) == 1
    assert f2.result(timeout=5) == 2


# ---------------------------------------------------------------------------
# Custom executor isolation — TaskGroup uses whatever Executor it's given
# ---------------------------------------------------------------------------


def test_taskgroup_uses_passed_executor():
    """The same TaskGroup should dispatch to whatever executor it was
    constructed with — not to any ambient or shared one."""
    seen_a: set[int] = set()
    seen_b: set[int] = set()
    seen_lock = threading.Lock()

    def record(sink: set[int]):
        with seen_lock:
            sink.add(threading.get_ident())

    with WorkerExecutor() as ex_a, WorkerExecutor() as ex_b:
        with TaskGroup(ex_a) as tg:
            for _ in range(4):
                tg.create_task(record, seen_a).result(timeout=5)
        with TaskGroup(ex_b) as tg:
            for _ in range(4):
                tg.create_task(record, seen_b).result(timeout=5)

    # Different executors → distinct worker thread sets.
    assert seen_a.isdisjoint(seen_b)
