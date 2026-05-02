"""``OpFilter`` — middleware that also contributes to the OpenAPI doc.

A filter is a callable that runs before the operation handler. It can
short-circuit the request by returning an :class:`OpResult` (e.g. a
``Unauthorized`` from auth) or return ``None`` to let the operation
proceed.

The OpenAPI improvement vs. FastAPI: the same object that runs at request
time also describes its contribution to the OpenAPI spec — security
schemes, extra parameters, extra response codes. There's no second place
to declare auth.

v1 ships only the protocol; concrete filters (``HttpBasicAuth``,
``HttpBearerAuth``, ``OpenIDConnectAuth``) come later.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from localpost.http.server import HTTPReqCtx
    from localpost.openapi import spec
    from localpost.openapi.results import OpResult


@runtime_checkable
class OpFilter(Protocol):
    """Pre-handler middleware that knows how to describe itself in OpenAPI.

    Filters can be applied app-wide (in :class:`HttpApp(filters=...)`) or
    per-operation (decorator on the same function as ``@app.get(...)``).
    Either way, the filter's :meth:`update_doc` is called when the spec is
    built so the doc stays consistent with the runtime behaviour.
    """

    def __call__(self, ctx: HTTPReqCtx, /) -> None | OpResult:
        """Run pre-handler. Return :class:`OpResult` to short-circuit, or
        ``None`` to let the operation proceed."""
        ...

    def update_doc(self, doc: spec.OpenAPI, op: spec.Operation | None = None, /) -> spec.OpenAPI:
        """Contribute to the OpenAPI doc.

        Called once at spec-build time. ``op`` is ``None`` for app-level
        filters (where the contribution is typically a
        ``components.securitySchemes`` entry) and the relevant operation
        for op-level filters (where the contribution is typically an
        extra response code and a ``security`` requirement).

        Returns a new :class:`spec.OpenAPI` (the doc is immutable).
        """
        ...
