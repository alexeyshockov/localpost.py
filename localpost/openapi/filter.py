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

For the common case — a filter built like a tiny operation, with
arg resolvers and a typed return — use :func:`op_filter`. It does the
signature inspection for you and the OpenAPI contribution comes from
the same annotations that run the resolvers::

    from localpost.openapi import FromHeader, Unauthorized, op_filter


    @op_filter
    def require_token(
        authorization: Annotated[str, FromHeader("Authorization")],
    ) -> None | Unauthorized[str]:
        if not authorization.startswith("Bearer "):
            return Unauthorized("Missing or malformed Authorization header")
        if not validate(authorization[7:]):
            return Unauthorized("Invalid token")
        return None


    @app.get("/me", filters=[require_token])
    def me() -> User: ...
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from localpost.http.server import HTTPReqCtx
    from localpost.openapi import spec
    from localpost.openapi.results import OpResult
    from localpost.openapi.schemas import SchemaRegistry

__all__ = ["OpFilter", "op_filter"]


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


@dataclass(eq=False, slots=True)
class _FunctionFilter:
    """Concrete :class:`OpFilter` produced by :func:`op_filter`.

    Inspects the wrapped function's signature once: arg resolvers come
    from the parameters (just like an :class:`Operation`), and the
    declared response codes come from the return-type union. The OpenAPI
    contribution mirrors what an operation with the same signature would
    emit.
    """

    target: Callable[..., Any]
    arg_resolvers: tuple[tuple[str, Any], ...]
    arg_factories: tuple[tuple[str, Any, Any | None], ...]
    return_shapes: tuple[Any, ...]

    # Optional root-level contribution (set by ``with_root``); auth filters
    # use this to register their securityScheme. Defaults to a no-op.
    root_contribution: Callable[[spec.OpenAPI, SchemaRegistry], spec.OpenAPI] | None = field(default=None)

    def __call__(self, ctx: HTTPReqCtx, /) -> None | OpResult:
        from localpost.openapi.results import OpResult as _OpResult  # noqa: PLC0415

        kwargs: dict[str, object] = {}
        for name, resolver in self.arg_resolvers:
            value = resolver(ctx)
            if isinstance(value, _OpResult):
                return value
            kwargs[name] = value
        result = self.target(**kwargs)
        if result is None or isinstance(result, _OpResult):
            return result
        name = getattr(self.target, "__qualname__", None) or repr(self.target)
        raise TypeError(
            f"@op_filter {name!r} returned {type(result).__name__}; filters must return None or an OpResult"
        )

    def contribute_root(self, doc: spec.OpenAPI, registry: SchemaRegistry, /) -> spec.OpenAPI:
        if self.root_contribution is None:
            return doc
        return self.root_contribution(doc, registry)

    def contribute_operation(self, op: spec.Operation, registry: SchemaRegistry, /) -> spec.Operation:
        from localpost.openapi.operation import _build_responses  # noqa: PLC0415

        # Parameters / requestBody come from the resolver factories.
        for _name, param, factory in self.arg_factories:
            if factory is None:
                continue
            op = factory.update_doc(param, op, registry)
        # Response codes come from the return-type union. Don't overwrite
        # codes the operation already declared (the op's own response is
        # more specific than the filter's generic one).
        for code, response in _build_responses(self.return_shapes, registry).items():
            if code in op.responses:
                continue
            op = replace(op, responses={**op.responses, code: response})
        return op

    def with_root(self, contribution: Callable[[spec.OpenAPI, SchemaRegistry], spec.OpenAPI]) -> _FunctionFilter:
        """Return a copy that also contributes ``contribution`` at the root.

        Used by auth filters to register their ``SecurityScheme`` while
        still inheriting the parameter / response contribution from the
        wrapped function's signature.
        """
        return replace(self, root_contribution=contribution)


def op_filter(fn: Callable[..., Any]) -> _FunctionFilter:
    """Wrap ``fn`` as an :class:`OpFilter`.

    The wrapped function looks like an operation handler: its parameters
    are resolved from the request (via :class:`FromHeader`,
    :class:`FromQuery`, :class:`FromBody`, …) and its return must be
    ``None`` (let the operation proceed) or an :class:`OpResult` (short-
    circuit with that response).

    The OpenAPI contribution is inferred from the same annotations: the
    declared parameters land on every operation that uses this filter,
    and the response codes from the return-type union are added to those
    operations' ``responses`` (without overwriting codes the operation
    already declared).
    """
    from localpost.openapi.operation import build_arg_resolvers, extract_response_shapes  # noqa: PLC0415

    sig, runtime, factories = build_arg_resolvers(fn)
    shapes_list, _null_is_not_found = extract_response_shapes(sig.return_annotation)
    return _FunctionFilter(
        target=fn,
        arg_resolvers=tuple(runtime),
        arg_factories=tuple(factories),
        return_shapes=tuple(shapes_list),
    )
