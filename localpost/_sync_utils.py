from __future__ import annotations

import socket
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass

import anyio
from anyio import from_thread

CHECK_TIMEOUT: float = 1.0
"""Timeout (seconds) for cancellation checks (e.g. in the server loop)."""


def check_cancelled() -> None:
    with suppress(anyio.NoEventLoopError):
        from_thread.check_cancelled()


def _acquire(sem: threading.Semaphore):
    while True:
        check_cancelled()
        if sem.acquire(timeout=CHECK_TIMEOUT):
            return
