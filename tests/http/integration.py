"""End-to-end integration tests for the HTTP flow.

Boots the full ``run`` + ``http_server`` stack in a subprocess (see
``_integration_app.py``), fires HTTP requests, and verifies clean shutdown
via SIGTERM and SIGINT, on both anyio backends, and across the router /
WSGI / Flask handlers.
"""

from __future__ import annotations

import concurrent.futures
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator

import httpx
import pytest

pytestmark = pytest.mark.integration


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(port: int, deadline: float = 10.0) -> bool:
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _spawn(port: int, *, backend: str = "asyncio", mode: str = "router", **extra_env: str) -> subprocess.Popen:
    env = {
        **os.environ,
        "LP_TEST_PORT": str(port),
        "LP_TEST_BACKEND": backend,
        "LP_TEST_MODE": mode,
        **extra_env,
    }
    return subprocess.Popen(  # noqa: S603
        [sys.executable, "-u", "-m", "tests.http._integration_app"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


@pytest.fixture
def app_process(request) -> Iterator[tuple[int, subprocess.Popen]]:
    """Start the integration app; parameterize via indirect request.param dict."""
    params: dict[str, str] = getattr(request, "param", {}) or {}
    port = _pick_free_port()
    proc = _spawn(port, **params)
    try:
        if not _wait_ready(port):
            stdout, stderr = b"", b""
            if proc.poll() is not None:
                stdout, stderr = proc.communicate(timeout=1)
            pytest.fail(
                f"Server did not become ready on port {port} (params={params}).\n"
                f"stdout: {stdout!r}\nstderr: {stderr!r}"
            )
        yield port, proc
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


# --- Router-handler integration (asyncio + trio) ------------------------------


@pytest.mark.parametrize("app_process", [{"backend": "asyncio"}, {"backend": "trio"}], indirect=True)
class TestRouterIntegration:
    def test_simple_get(self, app_process):
        port, _ = app_process
        r = httpx.get(f"http://127.0.0.1:{port}/ping", timeout=2)
        assert r.status_code == 200
        assert r.text == "pong"

    def test_path_params(self, app_process):
        port, _ = app_process
        r = httpx.get(f"http://127.0.0.1:{port}/hello/world", timeout=2)
        assert r.status_code == 200
        assert r.text == "hi world"

    def test_404(self, app_process):
        port, _ = app_process
        r = httpx.get(f"http://127.0.0.1:{port}/nonexistent", timeout=2)
        assert r.status_code == 404

    def test_concurrent_requests_span_multiple_threads(self, app_process):
        port, _ = app_process
        n = 8
        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
            futures = [
                ex.submit(lambda: httpx.get(f"http://127.0.0.1:{port}/slow", timeout=5)) for _ in range(n)
            ]
            responses = [f.result() for f in futures]

        assert all(r.status_code == 200 for r in responses)
        thread_ids = {r.text for r in responses}
        assert len(thread_ids) >= 2, f"expected multiple worker threads, saw: {thread_ids}"


# --- Shutdown signaling -------------------------------------------------------


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_clean_shutdown_via_signal(sig):
    port = _pick_free_port()
    proc = _spawn(port)
    try:
        assert _wait_ready(port), "server did not start"
        assert httpx.get(f"http://127.0.0.1:{port}/ping", timeout=2).status_code == 200

        proc.send_signal(sig)
        rc = proc.wait(timeout=5)
        assert rc == 0, f"process exited with code {rc} on {sig.name}"

        # Port should be free now.
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", port), timeout=1)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_inflight_request_during_shutdown_today():
    """SIGTERM while the slow handler is sleeping.

    Pin: today the worker thread is still in ``time.sleep`` when shutdown
    cancels the task group, and the eventual write/close races with the
    closed listener. The service exits non-zero. When in-flight handling
    is hardened (graceful drain), this test should flip to ``rc == 0``.
    """
    port = _pick_free_port()
    proc = _spawn(port, LP_TEST_SLOW_S="1.0")
    stderr = b""
    try:
        assert _wait_ready(port), "server did not start"

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(lambda: httpx.get(f"http://127.0.0.1:{port}/slow", timeout=5))
            time.sleep(0.2)  # request is in flight
            proc.send_signal(signal.SIGTERM)
            try:
                _, stderr = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                _, stderr = proc.communicate()
                raise
            rc = proc.returncode

            try:
                future.result(timeout=5)
            except (httpx.HTTPError, OSError):
                pass

        # Process must terminate within the deadline (no hangs). Exit code
        # is currently non-zero — see docstring.
        assert rc != 0, f"unexpected clean exit, stderr={stderr.decode()!r}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


# --- WSGI + Flask end-to-end smoke tests --------------------------------------


@pytest.mark.parametrize(
    "app_process",
    [{"mode": "wsgi"}, {"mode": "flask"}],
    indirect=True,
    ids=["wsgi", "flask"],
)
def test_alternate_handler_modes_serve(app_process):
    port, _ = app_process
    r = httpx.get(f"http://127.0.0.1:{port}/ping", timeout=2)
    assert r.status_code == 200
    # Both modes return non-empty bodies; exact text differs by mode.
    assert r.text
