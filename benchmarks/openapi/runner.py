"""Macro OpenAPI-framework benchmark runner — thin entry point.

Drives the same oha pipeline as ``benchmarks/http/runner.py`` but against
typed/OpenAPI frameworks (LocalPost, flask-openapi3, FastAPI) instead of
bare HTTP servers.

See :mod:`benchmarks._core.cli` for the CLI flag surface.
"""

from __future__ import annotations

import sys
from pathlib import Path

from benchmarks._core.cli import entrypoint
from benchmarks.openapi.scenarios import SCENARIOS
from benchmarks.openapi.stacks import DIM_KEYS, GROUPS, STACKS

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = Path(__file__).parent / "results"


def main() -> int:
    return entrypoint(
        scenarios=SCENARIOS,
        stacks=STACKS,
        groups=GROUPS,
        apps_pkg="benchmarks.openapi.apps",
        results_dir=RESULTS_DIR,
        title="OpenAPI framework benchmark",
        dim_keys=DIM_KEYS,
        repo_root=REPO_ROOT,
        description=__doc__,
    )


if __name__ == "__main__":
    sys.exit(main())
