"""Macro HTTP benchmark runner — thin entry point.

For each (python, stack, scenario) cell:
  1. Boot the stack as a subprocess (``<python> -m benchmarks.http.apps.<stack>``).
  2. Poll ``/ping`` until ready (or fail fast).
  3. Run ``oha --json -z <duration>s -c <conc> ...``.
  4. Parse JSON; record RPS + p50/p90/p99 + status histogram.
  5. SIGTERM the stack, wait, move on.

Output:
  results/<UTC-timestamp>/<python-label>/results.json — raw cells.
  results/<UTC-timestamp>/<python-label>/RESULTS.md  — markdown summary,
                                                       one table per scenario.
  results/<UTC-timestamp>/<python-label>/RESULTS.html — interactive view.

See :mod:`benchmarks._core.cli` for the CLI flag surface.
"""

from __future__ import annotations

import sys
from pathlib import Path

from benchmarks._core.cli import entrypoint
from benchmarks.http.scenarios import SCENARIOS
from benchmarks.http.stacks import DIM_KEYS, GROUPS, STACKS

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = Path(__file__).parent / "results"


def main() -> int:
    return entrypoint(
        scenarios=SCENARIOS,
        stacks=STACKS,
        groups=GROUPS,
        apps_pkg="benchmarks.http.apps",
        results_dir=RESULTS_DIR,
        title="HTTP benchmark",
        dim_keys=DIM_KEYS,
        repo_root=REPO_ROOT,
        description=__doc__,
    )


if __name__ == "__main__":
    sys.exit(main())
