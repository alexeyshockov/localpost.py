"""Shared fixtures for ``localpost.threadtools`` tests.

The new design makes ``thread_pool()`` mandatory for ``TaskGroup``. These
fixtures spin up a per-test pool via :func:`anyio.from_thread.start_blocking_portal`
so the existing sync test bodies don't need to be rewritten as async.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from anyio.from_thread import BlockingPortal, start_blocking_portal

from localpost.threadtools import ThreadPool, thread_pool
from localpost.threadtools._task_group import _current_pool


@pytest.fixture
def portal() -> Iterator[BlockingPortal]:
    """A blocking portal backing the ``pool`` fixture. Tests can use it
    directly when they need to call into async code from sync tests."""
    with start_blocking_portal() as p:
        yield p


@pytest.fixture(autouse=True)
def pool(request: pytest.FixtureRequest, portal: BlockingPortal) -> Iterator[ThreadPool | None]:
    """An ambient ``thread_pool`` for every test in this directory.
    Autouse so existing sync test bodies don't need to ask for it
    explicitly. Tests that *do* want introspection (``pool.idle``) can
    request it by name.

    The pool itself is opened on the portal's event loop, but the
    ``_current_pool`` contextvar is per-context — we explicitly mirror
    the binding into the test thread's context so sync ``TaskGroup()``
    constructions find the pool.

    Tests that manage their own pool lifecycle (typically async tests
    that use ``async with thread_pool():``) opt out via
    ``@pytest.mark.no_ambient_pool``."""
    if request.node.get_closest_marker("no_ambient_pool"):
        yield None
        return
    with portal.wrap_async_context_manager(thread_pool()) as p:
        token = _current_pool.set(p)
        try:
            yield p
        finally:
            _current_pool.reset(token)
