"""Macro HTTP benchmark runner.

For each (stack, scenario) cell:
  1. Boot the stack as a subprocess (``python -m benchmarks.http.apps.<stack>``).
  2. Poll ``/ping`` until ready (or fail fast).
  3. Run ``oha --json -z <duration>s -c <conc> ...``.
  4. Parse JSON; record RPS + p50/p90/p99 + status histogram.
  5. SIGTERM the stack, wait, move on.

Output:
  results/latest.json — raw cells.
  results/RESULTS.md  — markdown summary, one table per scenario.

Run from the repo root::

    just bench-http                              # full matrix
    just bench-http --duration 5                 # quick sanity
    just bench-http --stacks localpost_native,flask_gunicorn
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from benchmarks.http.scenarios import SCENARIOS, Scenario

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = Path(__file__).parent / "results"

STACKS: tuple[str, ...] = (
    "localpost_native",
    "localpost_wsgi",
    "localpost_flask",
    "flask_cheroot",
    "flask_gunicorn",
    "flask_granian",
    "starlette_uvicorn",
    "starlette_granian",
)


@dataclass(slots=True)
class Cell:
    stack: str
    scenario: str
    rps: float
    p50_ms: float
    p90_ms: float
    p99_ms: float
    total_requests: int
    success_rate: float
    status_2xx: int
    status_other: int


@dataclass(slots=True)
class RunReport:
    started_at: str
    duration_s: int
    host: str
    python: str
    cells: list[Cell]


def _pick_port(base: int, offset: int) -> int:
    # Naive: a fixed offset per stack invocation. Conflicts are rare in practice
    # and surface clearly via the readiness probe.
    return base + offset


def _wait_ready(port: int, deadline_s: float = 10.0) -> bool:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _spawn_stack(stack: str, port: int) -> subprocess.Popen:
    return subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", f"benchmarks.http.apps.{stack}", "--port", str(port)],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _run_oha(scenario: Scenario, port: int, duration_s: int) -> dict:
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


def _parse_oha(raw: dict) -> dict:
    summary = raw.get("summary", {})
    pct = raw.get("latencyPercentiles", {})
    status = raw.get("statusCodeDistribution", {})
    s2xx = sum(v for k, v in status.items() if k.startswith("2"))
    sother = sum(v for k, v in status.items() if not k.startswith("2"))
    total = s2xx + sother
    return {
        "rps": float(summary.get("requestsPerSec", 0.0)),
        "p50_ms": float(pct.get("p50", 0.0)) * 1000,
        "p90_ms": float(pct.get("p90", 0.0)) * 1000,
        "p99_ms": float(pct.get("p99", 0.0)) * 1000,
        "total_requests": total,
        "success_rate": float(summary.get("successRate", 0.0)),
        "status_2xx": s2xx,
        "status_other": sother,
    }


def _run_cell(stack: str, scenario: Scenario, port: int, duration_s: int) -> Cell | None:
    proc = _spawn_stack(stack, port)
    try:
        if not _wait_ready(port):
            stderr = proc.stderr.read().decode() if proc.stderr else ""
            print(f"  [{stack}/{scenario.name}] FAILED: not ready. stderr={stderr[-400:]!r}", file=sys.stderr)
            return None
        raw = _run_oha(scenario, port, duration_s)
        parsed = _parse_oha(raw)
        return Cell(stack=stack, scenario=scenario.name, **parsed)
    except Exception as e:  # noqa: BLE001
        print(f"  [{stack}/{scenario.name}] ERROR: {e}", file=sys.stderr)
        return None
    finally:
        _kill(proc)


def _write_results(report: RunReport) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "latest.json").write_text(json.dumps(asdict(report), indent=2) + "\n")
    (RESULTS_DIR / "RESULTS.md").write_text(_render_markdown(report))


def _render_markdown(report: RunReport) -> str:
    out: list[str] = []
    out.append("# HTTP benchmark results\n")
    out.append(f"- Run at: `{report.started_at}`")
    out.append(f"- Host: `{report.host}`")
    out.append(f"- Python: `{report.python}`")
    out.append(f"- Duration per cell: `{report.duration_s}s`")
    out.append("")
    out.append("> Numbers are single-process, single-host. Don't read absolute RPS as gospel —")
    out.append("> what matters is the relative ordering on the same machine in one run.")
    out.append("")

    cells_by_scenario: dict[str, list[Cell]] = {}
    for c in report.cells:
        cells_by_scenario.setdefault(c.scenario, []).append(c)

    for scenario_name, cells in cells_by_scenario.items():
        cells.sort(key=lambda c: c.rps, reverse=True)
        out.append(f"## {scenario_name}\n")
        out.append("| Stack | RPS | p50 (ms) | p90 (ms) | p99 (ms) | 2xx | non-2xx |")
        out.append("|---|---:|---:|---:|---:|---:|---:|")
        out.extend(
            f"| `{c.stack}` | {c.rps:,.0f} | {c.p50_ms:.2f} | {c.p90_ms:.2f} "
            f"| {c.p99_ms:.2f} | {c.status_2xx:,} | {c.status_other:,} |"
            for c in cells
        )
        out.append("")
    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--duration", type=int, default=20, help="seconds per cell (default: 20)")
    p.add_argument("--stacks", default="", help="comma-separated stack filter (default: all)")
    p.add_argument("--scenarios", default="", help="comma-separated scenario filter (default: all)")
    p.add_argument("--port-base", type=int, default=18800)
    args = p.parse_args()

    if shutil.which("oha") is None:
        print("error: 'oha' not found on PATH. Install via 'brew install oha'.", file=sys.stderr)
        return 2

    stacks = tuple(s for s in (args.stacks.split(",") if args.stacks else STACKS) if s)
    unknown = [s for s in stacks if s not in STACKS]
    if unknown:
        print(f"error: unknown stack(s): {unknown}. Known: {list(STACKS)}", file=sys.stderr)
        return 2

    scenarios = tuple(s for s in SCENARIOS if not args.scenarios or s.name in args.scenarios.split(","))
    if not scenarios:
        print("error: no scenarios selected.", file=sys.stderr)
        return 2

    cells: list[Cell] = []
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    print(f"Running {len(stacks)} stack(s) x {len(scenarios)} scenario(s) @ {args.duration}s each.")
    for stack_idx, stack in enumerate(stacks):
        for scen_idx, scenario in enumerate(scenarios):
            port = _pick_port(args.port_base, stack_idx * len(SCENARIOS) + scen_idx)
            print(f"  [{stack}/{scenario.name}] port={port} c={scenario.concurrency} ...", flush=True)
            cell = _run_cell(stack, scenario, port, args.duration)
            if cell is not None:
                print(
                    f"    rps={cell.rps:,.0f}  p50={cell.p50_ms:.2f}ms  p99={cell.p99_ms:.2f}ms  "
                    f"({cell.status_2xx} 2xx / {cell.status_other} other)"
                )
                cells.append(cell)

    report = RunReport(
        started_at=started_at,
        duration_s=args.duration,
        host=f"{platform.system()} {platform.release()} {platform.machine()}",
        python=sys.version.split()[0],
        cells=cells,
    )
    _write_results(report)
    print(f"\nWrote {RESULTS_DIR / 'RESULTS.md'} and {RESULTS_DIR / 'latest.json'}.")
    return 0 if cells else 1


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.exit(main())
