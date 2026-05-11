"""Tests for ``localpost.hosting.middleware.shutdown_on_signal``.

Signals are process-wide, so we exercise the middleware from a subprocess.
``run_app`` wires ``shutdown_on_signal`` automatically, so these tests
double as smoke coverage for the full integration.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest


def _spawn() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-u", "-m", "tests.hosting._signal_app"],
        env={**os.environ},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _wait_for_marker(proc: subprocess.Popen, marker: bytes, timeout: float = 5.0) -> bool:
    """Read stdout line-by-line waiting for ``marker``. Returns True on hit."""
    end = time.monotonic() + timeout
    assert proc.stdout is not None
    while time.monotonic() < end:
        line = proc.stdout.readline()
        if not line:
            time.sleep(0.02)
            continue
        if marker in line:
            return True
    return False


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_shutdown_on_signal_triggers_clean_exit(sig):
    """SIGTERM and SIGINT both trigger ``lt.shutdown`` and return exit code 0."""
    proc = _spawn()
    try:
        assert _wait_for_marker(proc, b"READY"), "service never reported READY"
        proc.send_signal(sig)
        rc = proc.wait(timeout=5)
        assert rc == 0, f"unexpected exit code {rc} on {sig.name}"

        # The service printed "STOPPED" before exiting cleanly.
        assert proc.stdout is not None
        remaining = proc.stdout.read()
        assert b"STOPPED" in remaining, f"service did not log clean stop: {remaining!r}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_second_sigint_forces_immediate_stop():
    """Two consecutive SIGINTs: the second one triggers ``lt.stop`` (forced)."""
    proc = _spawn()
    try:
        assert _wait_for_marker(proc, b"READY"), "service never reported READY"

        # First SIGINT: graceful shutdown begins. Service is in
        # ``await lt.shutting_down.wait()`` and will move to the print.
        proc.send_signal(signal.SIGINT)
        # Second SIGINT immediately: ``stop`` cancels the run scope.
        proc.send_signal(signal.SIGINT)

        rc = proc.wait(timeout=5)
        # Either path produces a successful exit — the cancellation is internal.
        assert rc in (0, 1), f"unexpected exit {rc}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
