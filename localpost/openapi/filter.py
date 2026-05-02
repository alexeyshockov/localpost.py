"""``OpFilter`` — middleware that also describes itself in the OpenAPI doc.

A filter is a callable that runs before the operation handler. It can
short-circuit the request by returning an :class:`OpResult` (e.g. a
``Unauthorized`` from auth) or return ``None`` to let the operation
proceed.

The OpenAPI improvement vs. FastAPI: the *same* object that runs at
request time also describes its contribution to the spec — security
schemes at the root, additional response codes / security requirements
on each operation. There's no second place to declare auth.

Two contribution hooks:

- :meth:`OpFilter.contribute_root` — called once at spec build time. The
  typical use is to register a :class:`SecurityScheme` under
  ``components.securitySchemes``.
- :meth:`OpFilter.contribute_operation` — called per operation that has
  this filter attached (app-level filters are attached to *every* op).
  The typical use is to add a 401 / 403 response and a ``security``
  requirement.

Filters typically override one or both of these; both have a default
implementation that returns the input unchanged so a filter that only
needs runtime behaviour can ignore the doc side.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from localpost.http.server import HTTPReqCtx
    from localpost.openapi import spec
    from localpost.openapi.results import OpResult
    from localpost.openapi.schemas import SchemaRegistry


@runtime_checkable
class OpFilter(Protocol):
    """Pre-handler middleware that knows how to describe itself in OpenAPI."""

    def __call__(self, ctx: HTTPReqCtx, /) -> None | OpResult:
        """Run pre-handler. Return :class:`OpResult` to short-circuit, or
        ``None`` to let the operation proceed."""
        ...

    def contribute_root(self, doc: spec.OpenAPI, registry: SchemaRegistry, /) -> spec.OpenAPI:
        """App-level OpenAPI contribution.

        Called once when the spec is built. Typical use: register a
        ``SecurityScheme`` under ``components.securitySchemes``. Default:
        return ``doc`` unchanged.
        """
        ...

    def contribute_operation(self, op: spec.Operation, registry: SchemaRegistry, /) -> spec.Operation:
        """Per-operation OpenAPI contribution.

        Called once per operation that this filter is attached to (every
        operation for app-level filters). Typical use: add a ``401`` /
        ``403`` response and a ``security`` requirement. Default: return
        ``op`` unchanged.
        """
        ...
