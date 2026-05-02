"""Bench stack registry — typed dimensions over the flat stack list.

Each stack is one row of the matrix. Rather than a flat tuple of names, each
stack carries explicit dimension fields (``app``, ``backend``, ``selectors``,
``pool``, ``acceptor``, ``tags``). The runner uses these to:

* select a subset for a run via ``--filter`` / ``--group`` / ``--stacks``;
* annotate each cell in ``results.json`` so the HTML reporter can pivot.

The ``name`` field doubles as the module name under ``benchmarks/http/apps/``,
so spawning a stack stays a one-liner — no app-module changes needed.

Filter language (parsed by :func:`parse_filters`):

* ``key=value`` — exact match. Multiple ``--filter`` flags AND together.
* ``key=a,b`` — comma list inside a key is OR.
* ``key=lp-*`` — glob via ``*`` / ``?`` (fnmatch).
* ``key!=value`` — negation (combines with comma + glob).
* Keys: ``app``, ``backend``, ``selectors``, ``pool``, ``acceptor``, ``tags``, ``name``.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Final


@dataclass(frozen=True, slots=True)
class Stack:
    name: str
    """Display id; matches ``benchmarks/http/apps/<name>.py``."""

    app: str
    """``native`` | ``wsgi`` | ``flask`` | ``starlette``."""

    backend: str
    """``lp-h11`` | ``lp-httptools`` | ``cheroot`` | ``gunicorn`` | ``granian`` | ``uvicorn``."""

    selectors: int
    """1 (single thread) | 4 (multi). Always 1 for non-LocalPost backends."""

    pool: bool
    """``False`` = handlers run inline on the selector thread."""

    acceptor: bool = False
    """``True`` = LocalPost acceptor topology (1 acceptor thread + N worker
    selectors via cross-thread op queue). Default ``False`` mirrors the
    ``selectors=N`` ``SO_REUSEPORT`` shape and applies to non-LocalPost
    backends as well."""

    tags: frozenset[str] = field(default_factory=frozenset)
    """Free-form labels, e.g. ``reference``."""


STACKS: Final[tuple[Stack, ...]] = (
    Stack("localpost_h11", app="native", backend="lp-h11", selectors=1, pool=True),
    Stack("localpost_h11_s4", app="native", backend="lp-h11", selectors=4, pool=True),
    Stack(
        "localpost_h11_acceptor_s4",
        app="native",
        backend="lp-h11",
        selectors=4,
        pool=True,
        acceptor=True,
    ),
    Stack("localpost_httptools", app="native", backend="lp-httptools", selectors=1, pool=True),
    Stack("localpost_httptools_s4", app="native", backend="lp-httptools", selectors=4, pool=True),
    Stack(
        "localpost_httptools_acceptor_s4",
        app="native",
        backend="lp-httptools",
        selectors=4,
        pool=True,
        acceptor=True,
    ),
    Stack("localpost_httptools_inline", app="native", backend="lp-httptools", selectors=1, pool=False),
    Stack("localpost_httptools_inline_s4", app="native", backend="lp-httptools", selectors=4, pool=False),
    Stack(
        "localpost_httptools_inline_acceptor_s4",
        app="native",
        backend="lp-httptools",
        selectors=4,
        pool=False,
        acceptor=True,
    ),
    Stack("localpost_wsgi", app="wsgi", backend="lp-h11", selectors=1, pool=True),
    Stack("localpost_flask", app="flask", backend="lp-h11", selectors=1, pool=True),
    Stack("flask_cheroot", app="flask", backend="cheroot", selectors=1, pool=True),
    Stack("flask_gunicorn", app="flask", backend="gunicorn", selectors=1, pool=True),
    Stack("flask_granian", app="flask", backend="granian", selectors=1, pool=True),
    Stack(
        "starlette_uvicorn",
        app="starlette",
        backend="uvicorn",
        selectors=1,
        pool=True,
        tags=frozenset({"reference"}),
    ),
    Stack(
        "starlette_granian",
        app="starlette",
        backend="granian",
        selectors=1,
        pool=True,
        tags=frozenset({"reference"}),
    ),
)

_STACKS_BY_NAME: Final[dict[str, Stack]] = {s.name: s for s in STACKS}


GROUPS: Final[dict[str, Callable[[Stack], bool]]] = {
    # Smallest sensible matrix — one representative per backend family.
    "quick": lambda s: s.name in {"localpost_httptools", "flask_granian", "flask_cheroot", "starlette_uvicorn"},
    "localpost": lambda s: s.backend.startswith("lp-"),
    "flask": lambda s: s.app == "flask",
    "reference": lambda s: "reference" in s.tags,
    "no-reference": lambda s: "reference" not in s.tags,
    "single-sel": lambda s: s.selectors == 1,
}


_VALID_KEYS: Final[frozenset[str]] = frozenset(
    {"app", "backend", "selectors", "pool", "acceptor", "tags", "name"}
)


@dataclass(frozen=True, slots=True)
class _Filter:
    key: str
    values: tuple[str, ...]
    """Each entry is an fnmatch pattern."""
    negate: bool


def parse_filters(specs: Iterable[str]) -> tuple[_Filter, ...]:
    """Parse ``key=v[,v]`` / ``key!=v[,v]`` filter strings.

    Raises ``ValueError`` with a helpful message on bad input.
    """
    out: list[_Filter] = []
    for raw in specs:
        spec = raw.strip()
        if not spec:
            continue
        negate = False
        if "!=" in spec:
            key, _, values = spec.partition("!=")
            negate = True
        elif "=" in spec:
            key, _, values = spec.partition("=")
        else:
            raise ValueError(f"--filter must be 'key=value' or 'key!=value', got: {spec!r}")
        key = key.strip()
        if key not in _VALID_KEYS:
            raise ValueError(f"unknown filter key {key!r}; valid keys: {sorted(_VALID_KEYS)}")
        items = tuple(v.strip() for v in values.split(",") if v.strip())
        if not items:
            raise ValueError(f"filter {spec!r} has no values")
        out.append(_Filter(key=key, values=items, negate=negate))
    return tuple(out)


def _stack_field(stack: Stack, key: str) -> tuple[str, ...]:
    if key == "tags":
        return tuple(sorted(stack.tags))
    if key == "selectors":
        return (str(stack.selectors),)
    if key == "pool":
        return ("true" if stack.pool else "false",)
    if key == "acceptor":
        return ("true" if stack.acceptor else "false",)
    return (str(getattr(stack, key)),)


def _match_filter(stack: Stack, f: _Filter) -> bool:
    haystack = _stack_field(stack, f.key)
    matched = any(fnmatch.fnmatchcase(h, pat) for h in haystack for pat in f.values)
    return not matched if f.negate else matched


def select_stacks(
    *,
    names: Iterable[str] | None = None,
    group: str | None = None,
    filters: Iterable[str] = (),
) -> tuple[Stack, ...]:
    """Resolve which stacks to run.

    Resolution order:

    1. If ``names`` is given, return those stacks verbatim (escape hatch;
       ``group`` and ``filters`` are ignored).
    2. Start from ``GROUPS[group]`` if set, else all of :data:`STACKS`.
    3. AND every parsed filter on top.

    Raises ``ValueError`` for unknown names / groups / filter keys.
    """
    if names:
        unknown = [n for n in names if n not in _STACKS_BY_NAME]
        if unknown:
            raise ValueError(f"unknown stack name(s): {unknown}. Known: {sorted(_STACKS_BY_NAME)}")
        return tuple(_STACKS_BY_NAME[n] for n in names)

    pool: tuple[Stack, ...] = STACKS
    if group is not None:
        if group not in GROUPS:
            raise ValueError(f"unknown group {group!r}; known: {sorted(GROUPS)}")
        pred = GROUPS[group]
        pool = tuple(s for s in pool if pred(s))

    parsed = parse_filters(filters)
    if parsed:
        pool = tuple(s for s in pool if all(_match_filter(s, f) for f in parsed))
    return pool
