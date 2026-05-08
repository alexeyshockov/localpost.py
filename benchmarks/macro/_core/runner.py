"""Cell-level orchestration: spawn → readiness probe → oha → parse → kill.

Suite-agnostic. The CLI passes in the suite's apps package prefix (e.g.
``benchmarks.macro.http.apps``) and the runner spawns ``python -m <prefix>.<stack>``
for each cell.
"""

from __future__ import annotations

import json
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from benchmarks.macro._core.types import Cell, Scenario, Stack


def pick_port(base: int, offset: int) -> int:
    # Naive: a fixed offset per stack invocation. Conflicts are rare in practice
    # and surface clearly via the readiness probe.
    return base + offset


def wait_ready(port: int, deadline_s: float = 10.0) -> bool:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def spawn_stack(apps_pkg: str, stack: str, port: int, python_bin: str, cwd: Path) -> subprocess.Popen:
    return subprocess.Popen(  # noqa: S603
        [python_bin, "-m", f"{apps_pkg}.{stack}", "--port", str(port)],
        cwd=cwd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def kill(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def run_oha(scenario: Scenario, port: int, duration_s: int) -> dict:
    base = f"http://127.0.0.1:{port}"
    cmd = [
        "oha",
        "--no-tui",
        "--output-format",
        "json",
        "-z",
        f"{duration_s}s",
        "-c",
        str(scenario.concurrency),
        "-m",
        scenario.method,
    ]
    if scenario.body is not None:
        cmd += ["-d", scenario.body.decode("utf-8")]
    if scenario.content_type is not None:
        cmd += ["-T", scenario.content_type]
    cmd.append(scenario.url(base))

    result = subprocess.run(  # noqa: S603
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=duration_s + 30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"oha exited {result.returncode}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def _as_float(v: float | int | str | None, default: float = 0.0) -> float:
    # oha emits explicit JSON null for percentiles when no responses fit the
    # bucket (e.g. flooded server, near-zero successful requests). dict.get's
    # default kicks in only for missing keys, not present-but-null, so coerce.
    return default if v is None else float(v)


def parse_oha(raw: dict, expected_status: int) -> dict:
    summary = raw.get("summary", {})
    pct = raw.get("latencyPercentiles", {})
    status = raw.get("statusCodeDistribution", {})
    expected_key = str(expected_status)
    s_expected = sum(v for k, v in status.items() if k == expected_key)
    s_other = sum(v for k, v in status.items() if k != expected_key)
    total = s_expected + s_other
    return {
        "rps": _as_float(summary.get("requestsPerSec")),
        "p50_ms": _as_float(pct.get("p50")) * 1000,
        "p90_ms": _as_float(pct.get("p90")) * 1000,
        "p99_ms": _as_float(pct.get("p99")) * 1000,
        "total_requests": total,
        "success_rate": _as_float(summary.get("successRate")),
        "status_expected": s_expected,
        "status_other": s_other,
    }


def run_cell(
    apps_pkg: str,
    stack: Stack,
    scenario: Scenario,
    port: int,
    duration_s: int,
    python_bin: str,
    cwd: Path,
) -> Cell | None:
    proc = spawn_stack(apps_pkg, stack.name, port, python_bin, cwd)
    try:
        if not wait_ready(port):
            stderr = proc.stderr.read().decode() if proc.stderr else ""
            print(f"  [{stack.name}/{scenario.name}] FAILED: not ready. stderr={stderr[-400:]!r}", file=sys.stderr)
            return None
        raw = run_oha(scenario, port, duration_s)
        parsed = parse_oha(raw, scenario.expected_status)
        return Cell(
            stack=stack.name,
            scenario=scenario.name,
            dims=dict(stack.dims),
            tags=sorted(stack.tags),
            **parsed,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  [{stack.name}/{scenario.name}] ERROR: {e}", file=sys.stderr)
        return None
    finally:
        kill(proc)


_PROBE_CODE = (
    "import sys;"
    "gil=getattr(sys,'_is_gil_enabled',lambda:True)();"
    'print(f\'{sys.version_info.major}.{sys.version_info.minor}{"" if gil else "t"}\');'
    "print(sys.version.split()[0])"
)


def probe_python(python_bin: str) -> tuple[str, str]:
    """Return (short label like ``3.14t``, full ``X.Y.Z`` version)."""
    out = subprocess.check_output([python_bin, "-c", _PROBE_CODE], text=True).strip().splitlines()  # noqa: S603
    return out[0], out[1]
