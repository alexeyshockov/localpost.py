"""Sync every venv in the bench matrix.

Iterates ``benchmarks.http._pythons.PYTHONS`` and runs ``uv sync`` for each,
redirected to the entry's venv via ``UV_PROJECT_ENVIRONMENT``. Continues on
failure; exits non-zero if any sync failed.

Only the groups + extras the HTTP benchmark stacks actually import are
installed — ``--all-groups --all-extras`` drags in native packages
(``grpcio-tools``, ``confluent-kafka``, ``google-cloud-pubsub``,
``opentelemetry-*``, …) that have spotty wheel coverage on free-threaded
Python (e.g. 3.14t).

Invoked by ``just bench-deps`` and ``just bench-deps-upgrade``.
"""

from __future__ import annotations

import os
import subprocess
import sys

from benchmarks.http._pythons import PYTHONS

# Just what the HTTP bench stacks need at runtime. See `benchmarks/http/apps/`
# for the actual imports.
GROUPS: tuple[str, ...] = (
    "bench",     # starlette, uvicorn, a2wsgi, cheroot, gunicorn, granian, pytest-benchmark
    "dev-http",  # flask, pydantic, httpx
)
EXTRAS: tuple[str, ...] = (
    "http",       # h11
    "http-fast",  # httptools
)


def _sync_one(venv: str, uv_python: str) -> int:
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": venv}
    cmd = ["uv", "sync", "--python", uv_python, "--no-default-groups"]
    for g in GROUPS:
        cmd += ["--group", g]
    for e in EXTRAS:
        cmd += ["--extra", e]
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
