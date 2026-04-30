"""Sync every venv in the bench matrix.

Iterates ``benchmarks.http._pythons.PYTHONS`` and runs
``uv sync --python <ver> --all-groups --all-extras`` for each, redirected
to the entry's venv via ``UV_PROJECT_ENVIRONMENT``. Continues on failure;
exits non-zero if any sync failed.

Invoked by ``just bench-deps`` and ``just bench-deps-upgrade``.
"""

from __future__ import annotations

import os
import subprocess
import sys

from benchmarks.http._pythons import PYTHONS


def _sync_one(venv: str, uv_python: str) -> int:
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": venv}
    cmd = ["uv", "sync", "--python", uv_python, "--all-groups", "--all-extras"]
    return subprocess.run(cmd, env=env, check=False).returncode  # noqa: S603


def main() -> int:
    failures: list[str] = []
    for py in PYTHONS:
        print(f"\n=== {py.name} ({py.venv}, --python {py.uv_python}) ===", flush=True)
        if _sync_one(py.venv, py.uv_python) != 0:
            failures.append(py.name)

    print()
    if failures:
        print(f"Failed: {', '.join(failures)}", file=sys.stderr)
        return 1
    print(f"OK ({len(PYTHONS)} venv(s) synced)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
