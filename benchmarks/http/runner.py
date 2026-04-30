"""Macro HTTP benchmark runner.

For each (python, stack, scenario) cell:
  1. Boot the stack as a subprocess (``<python> -m benchmarks.http.apps.<stack>``).
  2. Poll ``/ping`` until ready (or fail fast).
  3. Run ``oha --json -z <duration>s -c <conc> ...``.
  4. Parse JSON; record RPS + p50/p90/p99 + status histogram.
  5. SIGTERM the stack, wait, move on.

Output:
  results/<UTC-timestamp>/<python-label>/results.json — raw cells.
  results/<UTC-timestamp>/<python-label>/RESULTS.md  — markdown summary, one
                                                        table per scenario.

Each Python interpreter gets its own subdirectory; cross-interpreter numbers
are intentionally not merged (different runtimes are not directly comparable).

By default the runner targets every interpreter declared in
``benchmarks.http._pythons.PYTHONS``. Override with ``--pythons`` for ad-hoc
runs against a custom interpreter.

Run from the repo root::

    just bench-http                                       # full matrix
    just bench-http --duration 5                          # quick sanity
    just bench-http --group quick                         # ~4 stacks, fast PR check
    just bench-http --filter app=flask                    # all Flask servers
    just bench-http --filter 'backend=lp-*' --filter selectors=1
    just bench-http --stacks localpost_native,flask_gunicorn  # exact list (escape hatch)
    just bench-http --pythons 3.13=.venv-bench/3.13/bin/python
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
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from benchmarks.http._pythons import PYTHONS
from benchmarks.http._render_html import render_html
from benchmarks.http.scenarios import SCENARIOS, Scenario
from benchmarks.http.stacks import GROUPS, Stack, select_stacks

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = Path(__file__).parent / "results"


@dataclass(frozen=True, slots=True)
class PythonInterp:
    name: str
    bin: str


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
    # Dimensions copied from `Stack`, so results.json is self-describing
    # for the HTML reporter.
    app: str = ""
    backend: str = ""
    selectors: int = 1
    pool: bool = True
    tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RunReport:
    started_at: str
    duration_s: int
    host: str
    python: str
    python_version: str
    cells: list[Cell]
    selection: str = ""
    """Human-readable description of the stack selection (group/filter/stacks)."""


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


def _spawn_stack(stack: str, port: int, python_bin: str) -> subprocess.Popen:
    return subprocess.Popen(  # noqa: S603
        [python_bin, "-m", f"benchmarks.http.apps.{stack}", "--port", str(port)],
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


def _run_cell(stack: Stack, scenario: Scenario, port: int, duration_s: int, python_bin: str) -> Cell | None:
    proc = _spawn_stack(stack.name, port, python_bin)
    try:
        if not _wait_ready(port):
            stderr = proc.stderr.read().decode() if proc.stderr else ""
            print(f"  [{stack.name}/{scenario.name}] FAILED: not ready. stderr={stderr[-400:]!r}", file=sys.stderr)
            return None
        raw = _run_oha(scenario, port, duration_s)
        parsed = _parse_oha(raw)
        return Cell(
            stack=stack.name,
            scenario=scenario.name,
            app=stack.app,
            backend=stack.backend,
            selectors=stack.selectors,
            pool=stack.pool,
            tags=sorted(stack.tags),
            **parsed,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  [{stack.name}/{scenario.name}] ERROR: {e}", file=sys.stderr)
        return None
    finally:
        _kill(proc)


def _write_results(report: RunReport, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    payload = asdict(report)
    (target_dir / "results.json").write_text(json.dumps(payload, indent=2) + "\n")
    (target_dir / "RESULTS.md").write_text(_render_markdown(report))
    (target_dir / "RESULTS.html").write_text(render_html(payload))


def _render_markdown(report: RunReport) -> str:
    out: list[str] = []
    out.append(f"# HTTP benchmark results — Python {report.python}\n")
    out.append(f"- Run at: `{report.started_at}`")
    out.append(f"- Host: `{report.host}`")
    out.append(f"- Python: `{report.python}` (`{report.python_version}`)")
    out.append(f"- Duration per cell: `{report.duration_s}s`")
    if report.selection:
        out.append(f"- Selection: {report.selection}")
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


_PROBE_CODE = (
    "import sys;"
    "gil=getattr(sys,'_is_gil_enabled',lambda:True)();"
    "print(f'{sys.version_info.major}.{sys.version_info.minor}{\"\" if gil else \"t\"}');"
    "print(sys.version.split()[0])"
)


def _probe_python(python_bin: str) -> tuple[str, str]:
    """Return (short label like ``3.14t``, full ``X.Y.Z`` version)."""
    out = subprocess.check_output([python_bin, "-c", _PROBE_CODE], text=True).strip().splitlines()  # noqa: S603
    return out[0], out[1]


def _parse_pythons(arg: str) -> list[PythonInterp]:
    """Parse ``name=path,name=path`` (or fall back to the bench matrix)."""
    if not arg:
        return [PythonInterp(name=p.name, bin=p.bin) for p in PYTHONS]
    pythons: list[PythonInterp] = []
    for spec in arg.split(","):
        spec = spec.strip()
        if not spec:
            continue
        if "=" not in spec:
            raise ValueError(f"--pythons entry must be name=path, got: {spec!r}")
        name, _, path = spec.partition("=")
        pythons.append(PythonInterp(name=name.strip(), bin=path.strip()))
    return pythons


def _timestamp_slug() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S")


def _describe_selection(args: argparse.Namespace) -> str:
    parts: list[str] = []
    if args.stacks:
        parts.append(f"stacks={args.stacks}")
    if args.group:
        parts.append(f"group={args.group}")
    if args.filter:
        parts.append("filter=" + ", ".join(args.filter))
    return "; ".join(parts) if parts else "all"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--duration", type=int, default=20, help="seconds per cell (default: 20)")
    p.add_argument(
        "--stacks",
        default="",
        help="comma-separated stack name(s) — verbatim escape hatch (bypasses --group/--filter)",
    )
    p.add_argument(
        "--group",
        default="",
        help=f"named preset; one of: {', '.join(sorted(GROUPS))}",
    )
    p.add_argument(
        "--filter",
        action="append",
        default=[],
        metavar="KEY=VAL",
        help="dimension filter, e.g. 'app=flask', 'backend=lp-*', 'selectors=1', 'app!=starlette'. "
        "Repeatable (filters AND together). Comma-separated values inside one key are OR.",
    )
    p.add_argument("--scenarios", default="", help="comma-separated scenario filter (default: all)")
    p.add_argument(
        "--pythons",
        default="",
        help="comma-separated name=path pairs to override the bench matrix, e.g. "
        "'3.13=.venv-bench/3.13/bin/python' (default: every entry in benchmarks.http._pythons.PYTHONS)",
    )
    p.add_argument("--port-base", type=int, default=18800)
    args = p.parse_args()

    if shutil.which("oha") is None:
        print("error: 'oha' not found on PATH. Install via 'brew install oha'.", file=sys.stderr)
        return 2

    try:
        stacks = select_stacks(
            names=tuple(s for s in args.stacks.split(",") if s) if args.stacks else None,
            group=args.group or None,
            filters=args.filter,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not stacks:
        print("error: no stacks selected by the given group/filter.", file=sys.stderr)
        return 2

    scenarios = tuple(s for s in SCENARIOS if not args.scenarios or s.name in args.scenarios.split(","))
    if not scenarios:
        print("error: no scenarios selected.", file=sys.stderr)
        return 2

    try:
        pythons = _parse_pythons(args.pythons)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    for py in pythons:
        if not Path(py.bin).exists():
            print(f"error: interpreter not found: {py.bin}", file=sys.stderr)
            return 2

    run_dir = RESULTS_DIR / _timestamp_slug()
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    host = f"{platform.system()} {platform.release()} {platform.machine()}"
    selection = _describe_selection(args)

    print(
        f"Running {len(pythons)} python(s) x {len(stacks)} stack(s) "
        f"x {len(scenarios)} scenario(s) @ {args.duration}s each."
    )
    print(f"Selection: {selection}")
    print(f"Results dir: {run_dir}")

    any_cells = False
    for py in pythons:
        try:
            _, full_version = _probe_python(py.bin)
        except (subprocess.CalledProcessError, OSError) as e:
            print(f"error: cannot probe {py.bin}: {e}", file=sys.stderr)
            continue
        print(f"\n=== python={py.name} ({full_version}) ===")
        cells: list[Cell] = []
        for stack_idx, stack in enumerate(stacks):
            for scen_idx, scenario in enumerate(scenarios):
                port = _pick_port(args.port_base, stack_idx * len(SCENARIOS) + scen_idx)
                print(
                    f"  [{py.name}/{stack.name}/{scenario.name}] port={port} c={scenario.concurrency} ...",
                    flush=True,
                )
                cell = _run_cell(stack, scenario, port, args.duration, py.bin)
                if cell is not None:
                    print(
                        f"    rps={cell.rps:,.0f}  p50={cell.p50_ms:.2f}ms  p99={cell.p99_ms:.2f}ms  "
                        f"({cell.status_2xx} 2xx / {cell.status_other} other)"
                    )
                    cells.append(cell)

        report = RunReport(
            started_at=started_at,
            duration_s=args.duration,
            host=host,
            python=py.name,
            python_version=full_version,
            cells=cells,
            selection=selection,
        )
        target_dir = run_dir / py.name
        _write_results(report, target_dir)
        print(f"  wrote {target_dir / 'RESULTS.md'}")
        if cells:
            any_cells = True

    return 0 if any_cells else 1


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.exit(main())
