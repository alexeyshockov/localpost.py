"""Shared argparse + main loop for macro bench runners.

Each suite's ``runner.py`` is a thin entry point that calls :func:`run`
with its own scenarios, stacks, groups, and apps package.

Run from the repo root::

    just bench-http                                       # full matrix
    just bench-http --duration 5                          # quick sanity
    just bench-http --group quick                         # ~4 stacks, fast PR check
    just bench-http --filter app=flask                    # all Flask servers
    just bench-http --filter 'backend=lp-*' --filter selectors=1
    just bench-http --stacks localpost_h11,flask_gunicorn  # exact list (escape hatch)
    just bench-http --pythons 3.13=.venv-bench/3.13/bin/python
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from benchmarks._core.filters import select_stacks
from benchmarks._core.pythons import PYTHONS
from benchmarks._core.render import render_html, render_markdown
from benchmarks._core.runner import pick_port, probe_python, run_cell
from benchmarks._core.types import Cell, RunReport, Scenario, Stack


@dataclass(frozen=True, slots=True)
class PythonInterp:
    name: str
    bin: str


def _write_results(report: RunReport, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    payload = asdict(report)
    (target_dir / "results.json").write_text(json.dumps(payload, indent=2) + "\n")
    (target_dir / "RESULTS.md").write_text(render_markdown(report))
    (target_dir / "RESULTS.html").write_text(render_html(payload))


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


def run(
    *,
    scenarios: tuple[Scenario, ...],
    stacks: tuple[Stack, ...],
    groups: dict[str, Callable[[Stack], bool]],
    apps_pkg: str,
    results_dir: Path,
    title: str,
    dim_keys: Iterable[str],
    repo_root: Path,
    argv: list[str] | None = None,
    description: str | None = None,
) -> int:
    """Drive a full matrix run for one suite.

    Args:
        scenarios: Suite's scenario list.
        stacks: Suite's stack list.
        groups: Named stack-selection presets.
        apps_pkg: Python package prefix used to spawn each stack as
            ``python -m <apps_pkg>.<stack_name>``.
        results_dir: Where to write per-run output directories.
        title: Used in markdown / HTML headers.
        dim_keys: Ordered dim names — drives HTML column order and filters.
            Cells dynamically carry their own dim values; this controls
            display ordering only.
        repo_root: Working directory for spawned subprocesses.
        argv: Override sys.argv[1:] (for testing).
        description: argparse description.
    """
    p = argparse.ArgumentParser(description=description, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--duration", type=int, default=20, help="seconds per cell (default: 20)")
    p.add_argument(
        "--stacks",
        default="",
        help="comma-separated stack name(s) — verbatim escape hatch (bypasses --group/--filter)",
    )
    p.add_argument(
        "--group",
        default="",
        help=f"named preset; one of: {', '.join(sorted(groups))}" if groups else "(no groups defined)",
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
        "'3.13=.venv-bench/3.13/bin/python' (default: every entry in benchmarks._core.pythons.PYTHONS)",
    )
    p.add_argument("--port-base", type=int, default=18800)
    args = p.parse_args(argv)

    if shutil.which("oha") is None:
        print("error: 'oha' not found on PATH. Install via 'brew install oha'.", file=sys.stderr)
        return 2

    try:
        selected_stacks = select_stacks(
            stacks,
            groups,
            names=tuple(s for s in args.stacks.split(",") if s) if args.stacks else None,
            group=args.group or None,
            filters=args.filter,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not selected_stacks:
        print("error: no stacks selected by the given group/filter.", file=sys.stderr)
        return 2

    selected_scenarios = tuple(s for s in scenarios if not args.scenarios or s.name in args.scenarios.split(","))
    if not selected_scenarios:
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

    run_dir = results_dir / _timestamp_slug()
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    host = f"{platform.system()} {platform.release()} {platform.machine()}"
    selection = _describe_selection(args)
    dim_keys_list = list(dim_keys)

    print(
        f"Running {len(pythons)} python(s) x {len(selected_stacks)} stack(s) "
        f"x {len(selected_scenarios)} scenario(s) @ {args.duration}s each."
    )
    print(f"Selection: {selection}")
    print(f"Results dir: {run_dir}")

    any_cells = False
    for py in pythons:
        try:
            _, full_version = probe_python(py.bin)
        except (subprocess.CalledProcessError, OSError) as e:
            print(f"error: cannot probe {py.bin}: {e}", file=sys.stderr)
            continue
        print(f"\n=== python={py.name} ({full_version}) ===")
        cells: list[Cell] = []
        for stack_idx, stack in enumerate(selected_stacks):
            for scen_idx, scenario in enumerate(selected_scenarios):
                # NB: offset uses len(scenarios), not len(selected_scenarios), so partial
                # --scenarios runs still pick distinct ports per cell.
                port = pick_port(args.port_base, stack_idx * len(scenarios) + scen_idx)
                print(
                    f"  [{py.name}/{stack.name}/{scenario.name}] port={port} c={scenario.concurrency} ...",
                    flush=True,
                )
                cell = run_cell(apps_pkg, stack, scenario, port, args.duration, py.bin, repo_root)
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
            title=title,
            dim_keys=dim_keys_list,
        )
        target_dir = run_dir / py.name
        _write_results(report, target_dir)
        print(f"  wrote {target_dir / 'RESULTS.md'}")
        if cells:
            any_cells = True

    return 0 if any_cells else 1


def entrypoint(
    *,
    scenarios: tuple[Scenario, ...],
    stacks: tuple[Stack, ...],
    groups: dict[str, Callable[[Stack], bool]],
    apps_pkg: str,
    results_dir: Path,
    title: str,
    dim_keys: Iterable[str],
    repo_root: Path,
    description: str | None = None,
) -> int:
    """``__main__`` wrapper that sets ``PYTHONUNBUFFERED`` and forwards to :func:`run`."""
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    return run(
        scenarios=scenarios,
        stacks=stacks,
        groups=groups,
        apps_pkg=apps_pkg,
        results_dir=results_dir,
        title=title,
        dim_keys=dim_keys,
        repo_root=repo_root,
        description=description,
    )
