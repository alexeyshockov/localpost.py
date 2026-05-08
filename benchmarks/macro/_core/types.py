"""Shared dataclasses for macro bench runners.

Each suite (http, openapi) builds its own ``SCENARIOS`` and ``STACKS`` from
these types. The runner, filter language, and report writers operate on the
:class:`Stack`'s ``dims`` mapping uniformly — what dim keys each suite
chooses (``app``/``backend`` for http, ``framework``/``server`` for openapi)
is up to the suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Scenario:
    """One wire contract every stack must implement."""

    name: str
    method: str
    path: str
    body: bytes | None
    content_type: str | None
    expected_status: int
    concurrency: int
    """oha -c value."""

    def url(self, base: str) -> str:
        return f"{base}{self.path}"


@dataclass(frozen=True, slots=True)
class Stack:
    """One row of the bench matrix.

    ``name`` doubles as the module name under the suite's ``apps/`` package
    — the runner spawns it as ``python -m <apps_pkg>.<name>``.

    ``dims`` carries suite-specific dimensions used by ``--filter`` and the
    HTML reporter. Values are stored as strings so the filter language and
    HTML dropdowns can share one code path.

    ``tags`` is a free-form multi-valued set with its own filter semantics
    (matched element-wise against ``--filter tags=...``).
    """

    name: str
    dims: dict[str, str] = field(default_factory=dict)
    tags: frozenset[str] = field(default_factory=frozenset)


@dataclass(slots=True)
class Cell:
    """One (stack, scenario) result."""

    stack: str
    scenario: str
    rps: float
    p50_ms: float
    p90_ms: float
    p99_ms: float
    total_requests: int
    success_rate: float
    status_expected: int
    """Responses matching :attr:`Scenario.expected_status` exactly."""

    status_other: int
    """Everything else (wrong status code, no response, etc.)."""

    dims: dict[str, str] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RunReport:
    """One run's worth of cells, plus metadata for the report writers."""

    started_at: str
    duration_s: int
    host: str
    python: str
    python_version: str
    cells: list[Cell]
    selection: str = ""
    """Human-readable description of the stack selection (group/filter/stacks)."""

    title: str = "Benchmark"
    """Report title (e.g. 'HTTP benchmark', 'OpenAPI benchmark')."""

    dim_keys: list[str] = field(default_factory=list)
    """Ordered dim names — drives HTML filter dropdowns and column ordering."""
