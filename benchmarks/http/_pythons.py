"""Bench-matrix Python interpreters.

Single source of truth for which Python interpreters the HTTP benchmark
runs against. Read by both ``_setup.py`` (to provision each venv) and
``runner.py`` (as the default value for ``--pythons``).

Bench venvs live in ``.venv-bench/<name>/`` — fully separate from the
project's primary ``.venv``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BenchPython:
    name: str
    venv: str
    uv_python: str

    @property
    def bin(self) -> str:
        return f"{self.venv}/bin/python"


PYTHONS: tuple[BenchPython, ...] = (
    BenchPython(name="3.13", venv=".venv-bench/3.13", uv_python="3.13"),
    BenchPython(name="3.14t", venv=".venv-bench/3.14t", uv_python="3.14t"),
)
