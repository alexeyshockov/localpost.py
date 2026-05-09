"""Shared fixtures for ``localpost.threadtools`` tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from localpost.threadtools import WorkerExecutor


@pytest.fixture
def executor() -> Iterator[WorkerExecutor]:
    """A per-test :class:`WorkerExecutor` opened as a sync context manager."""
    with WorkerExecutor() as ex:
        yield ex
