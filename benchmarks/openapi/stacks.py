"""OpenAPI bench stack registry.

Three stacks for v1 — one per peer framework, each on its naturally-fitting
server. Single-process by design so we measure framework overhead, not
worker multiplexing.

* ``localpost_openapi``  — ``localpost.openapi.HttpApp`` on ``localpost.http`` (h11).
* ``flask_openapi``      — ``flask-openapi3`` on Gunicorn (1 worker, 32 threads).
* ``fastapi``            — FastAPI on Uvicorn (1 worker).

Dim keys: ``framework``, ``server``, ``schema`` (the validation library
each framework drives — ``msgspec`` for LocalPost, ``pydantic`` for the
other two).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from benchmarks._core.types import Stack

DIM_KEYS: Final[tuple[str, ...]] = ("framework", "server", "schema")


def _stack(name: str, *, framework: str, server: str, schema: str) -> Stack:
    return Stack(
        name=name,
        dims={"framework": framework, "server": server, "schema": schema},
    )


STACKS: Final[tuple[Stack, ...]] = (
    _stack("localpost_openapi", framework="localpost", server="lp-h11", schema="msgspec"),
    _stack("flask_openapi", framework="flask-openapi3", server="gunicorn", schema="pydantic"),
    _stack("fastapi", framework="fastapi", server="uvicorn", schema="pydantic"),
)


GROUPS: Final[dict[str, Callable[[Stack], bool]]] = {
    "localpost": lambda s: s.dims["framework"] == "localpost",
    "peers": lambda s: s.dims["framework"] != "localpost",
}
