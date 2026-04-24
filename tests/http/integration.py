"""End-to-end integration test for the HTTP flow.

Boots the full ``run_app`` + ``http_server`` + ``Router`` stack in a subprocess,
fires concurrent HTTP requests, confirms requests are served from multiple worker
threads, and then verifies a clean shutdown via SIGTERM.
"""

from __future__ import annotations

import concurrent.futures
import os
import signal
import socket
import subprocess
import sys
import textwrap
import time

import httpx
import pytest

pytestmark = pytest.mark.integration


_APP_SCRIPT = textwrap.dedent(
    """
    import logging
    import os
    import sys
    import threading
    import time

    from localpost.hosting import run_app
    from localpost.http import RequestCtx, Response, Routes, ServerConfig, http_server


    logging.basicConfig(level=logging.INFO)


    def _root(_: RequestCtx) -> Response:
        return Response(200, {"content-type": "text/plain"}, [b"pong"])


    def _slow(_: RequestCtx) -> Response:
        time.sleep(0.2)
        body = str(threading.get_ident()).encode()
        return Response(200, {"content-type": "text/plain"}, [body])


    def _hello(ctx: RequestCtx) -> Response:
        return Response(
            200,
            {"content-type": "text/plain"},
            [f"hi {ctx.path_args['name']}".encode()],
        )


    routes = Routes()
    routes.get("/ping")(_root)
    routes.get("/slow")(_slow)
    routes.get("/hello/{name}")(_hello)
    router = routes.build()

    port = int(os.environ["LP_TEST_PORT"])
    cfg = ServerConfig(host="127.0.0.1", port=port)
    sys.exit(run_app(http_server(cfg, router.as_handler(), max_concurrency=8)))
    """
)


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


@pytest.fixture
def app_process():
    port = _pick_free_port()
    env = {**os.environ, "LP_TEST_PORT": str(port)}
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-u", "-c", _APP_SCRIPT],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        if not _wait_ready(port):
            stdout, stderr = b"", b""
            if proc.poll() is not None:
                stdout, stderr = proc.communicate(timeout=1)
            pytest.fail(
                f"Server did not become ready on port {port}.\n"
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


class TestHttpIntegration:
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
            futures = [ex.submit(lambda: httpx.get(f"http://127.0.0.1:{port}/slow", timeout=5)) for _ in range(n)]
            responses = [f.result() for f in futures]

        assert all(r.status_code == 200 for r in responses)
        thread_ids = {r.text for r in responses}
        assert len(thread_ids) >= 2, f"expected multiple worker threads, saw: {thread_ids}"

    def test_clean_shutdown_via_sigterm(self, app_process):
        port, proc = app_process

        # Sanity check
        assert httpx.get(f"http://127.0.0.1:{port}/ping", timeout=2).status_code == 200

        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=5)
        assert rc == 0, f"process exited with code {rc}"

        # Port should be free now — expect ConnectionRefusedError once the server is down.
        with pytest.raises(ConnectionRefusedError), socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
