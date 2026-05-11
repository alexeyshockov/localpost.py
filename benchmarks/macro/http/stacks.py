"""HTTP bench stack registry — typed dimensions over the flat stack list.

Each stack is one row of the matrix. Rather than a flat tuple of names,
each stack carries explicit ``dims`` — ``app``, ``backend``, ``selectors``,
``pool``, ``acceptor`` — and an optional ``tags`` set. The runner uses
these to:

* select a subset for a run via ``--filter`` / ``--group`` / ``--stacks``;
* annotate each cell in ``results.json`` so the HTML reporter can pivot.

The ``name`` field doubles as the module name under
``benchmarks/macro/http/apps/``, so spawning a stack stays a one-liner — no
app-module changes needed.

See :mod:`benchmarks.macro._core.filters` for the filter language.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from benchmarks.macro._core.types import Stack

# Ordered dim keys — drives HTML column ordering and filter dropdown order.
DIM_KEYS: Final[tuple[str, ...]] = ("app", "backend", "selectors", "pool", "acceptor")


def _stack(
    name: str,
    *,
    app: str,
    backend: str,
    selectors: int = 1,
    pool: bool = True,
    acceptor: bool = False,
    tags: frozenset[str] = frozenset(),
) -> Stack:
    return Stack(
        name=name,
        dims={
            "app": app,
            "backend": backend,
            "selectors": str(selectors),
            "pool": "true" if pool else "false",
            "acceptor": "true" if acceptor else "false",
        },
        tags=tags,
    )


STACKS: Final[tuple[Stack, ...]] = (
    _stack("localpost_h11", app="native", backend="lp-h11"),
    _stack("localpost_h11_s4", app="native", backend="lp-h11", selectors=4),
    _stack("localpost_h11_acceptor_s4", app="native", backend="lp-h11", selectors=4, acceptor=True),
    _stack("localpost_httptools", app="native", backend="lp-httptools"),
    _stack("localpost_httptools_s4", app="native", backend="lp-httptools", selectors=4),
    _stack(
        "localpost_httptools_acceptor_s4",
        app="native",
        backend="lp-httptools",
        selectors=4,
        acceptor=True,
    ),
    _stack("localpost_httptools_inline", app="native", backend="lp-httptools", pool=False),
    _stack(
        "localpost_httptools_inline_s4",
        app="native",
        backend="lp-httptools",
        selectors=4,
        pool=False,
    ),
    _stack(
        "localpost_httptools_inline_acceptor_s4",
        app="native",
        backend="lp-httptools",
        selectors=4,
        pool=False,
        acceptor=True,
    ),
    _stack("localpost_wsgi", app="wsgi", backend="lp-h11"),
    _stack("localpost_flask", app="flask", backend="lp-h11"),
    _stack("flask_cheroot", app="flask", backend="cheroot"),
    _stack("flask_gunicorn", app="flask", backend="gunicorn"),
    _stack("flask_granian", app="flask", backend="granian"),
    _stack("starlette_uvicorn", app="starlette", backend="uvicorn", tags=frozenset({"reference"})),
    _stack("starlette_granian", app="starlette", backend="granian", tags=frozenset({"reference"})),
)


GROUPS: Final[dict[str, Callable[[Stack], bool]]] = {
    # Smallest sensible matrix — one representative per backend family.
    "quick": lambda s: s.name in {"localpost_httptools", "flask_granian", "flask_cheroot", "starlette_uvicorn"},
    "localpost": lambda s: s.dims["backend"].startswith("lp-"),
    "flask": lambda s: s.dims["app"] == "flask",
    "reference": lambda s: "reference" in s.tags,
    "no-reference": lambda s: "reference" not in s.tags,
    "single-sel": lambda s: s.dims["selectors"] == "1",
}
