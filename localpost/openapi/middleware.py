"""``OpMiddleware`` — middleware that also describes itself in the OpenAPI doc.

A middleware wraps an :class:`~localpost.openapi.operation.Operation`'s
core. It receives the request context plus a ``call_next`` callable that
runs the rest of the chain (next middleware, then the operation itself).
A middleware can:

- Short-circuit by returning an :class:`OpResult` without invoking
  ``call_next`` — the typical auth pattern.
- Forward to the chain (``return call_next(ctx)``) and return its result
  unchanged — pure observers / metrics.
- Forward, then post-process the :class:`OpResult` — add headers, swap
  the body, wrap an SSE stream, etc.

The OpenAPI improvement vs. plain wrap-and-forget middleware: the *same*
object that runs at request time also describes its contribution to the
spec — security schemes at the root, additional response codes / security
requirements / parameters on each operation. There's no second place to
declare auth.

Two contribution hooks:

- :meth:`OpMiddleware.contribute_root` — called once at spec build time.
  Typical use: register a :class:`SecurityScheme` under
  ``components.securitySchemes``.
- :meth:`OpMiddleware.contribute_operation` — called per operation that
  has this middleware attached (app-level middlewares are attached to
  *every* op). Typical use: add a 401 / 403 response, a ``security``
  requirement, or an extra header parameter.

For the common case — a middleware built like a tiny operation, with arg
resolvers and a typed return — use :func:`op_middleware`. It does the
signature inspection for you and the OpenAPI contribution comes from the
same annotations that run the resolvers::

    from localpost.openapi import FromHeader, Unauthorized, op_middleware


    @op_middleware
    def require_token(
        ctx: HTTPReqCtx,
        call_next: ApiOperation,
        authorization: Annotated[str, FromHeader("Authorization")] = "",
    ) -> Unauthorized[str] | OpResult:
        if not authorization.startswith("Bearer "):
            return Unauthorized("Missing or malformed Authorization header")
        if not validate(authorization[7:]):
            return Unauthorized("Invalid token")
        return call_next(ctx)


    @app.get("/me", middlewares=[require_token])
    def me() -> User: ...

Bare ``OpResult`` in the return-type union is the *passthrough* sentinel:
it represents the wrapped operation's own result and contributes nothing
to the spec. Subclasses (``Unauthorized[str]`` etc.) add their response
codes to every operation that uses the middleware.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from localpost.openapi.results import OpResult

if TYPE_CHECKING:
    from localpost.http import HTTPReqCtx
    from localpost.openapi import spec
    from localpost.openapi.schemas import SchemaRegistry

__all__ = ["ApiOperation", "OpMiddleware", "op_middleware"]


type ApiOperation = Callable[["HTTPReqCtx"], "OpResult"]
"""The shape of an operation as seen by middleware.

A middleware's ``call_next`` is an :class:`ApiOperation` — given the
request context, it returns an :class:`OpResult` (which may be an
:class:`~localpost.openapi.results.EventStreamResult` for SSE handlers).
"""


@runtime_checkable
class OpMiddleware(Protocol):
    """Operation middleware that knows how to describe itself in OpenAPI."""

    def __call__(self, ctx: HTTPReqCtx, call_next: ApiOperation, /) -> OpResult:
        """Run around the operation. Return an :class:`OpResult` —
        typically by calling ``call_next(ctx)`` and forwarding (or
        post-processing) its result, or by short-circuiting with a
        middleware-specific result (e.g. ``Unauthorized``)."""
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

        Called once per operation that this middleware is attached to
        (every operation for app-level middlewares). Typical use: add a
        ``401`` / ``403`` response and a ``security`` requirement.
        Default: return ``op`` unchanged.
        """
        ...


@dataclass(eq=False, slots=True)
class _FunctionMiddleware:
    """Concrete :class:`OpMiddleware` produced by :func:`op_middleware`.

    Inspects the wrapped function's signature once: arg resolvers come
    from the parameters (just like an :class:`Operation`), the
    ``call_next`` parameter is identified and threaded through at request
    time, and the declared response codes come from the return-type
    union. The OpenAPI contribution mirrors what an operation with the
    same signature would emit.
    """

    target: Callable[..., Any]
    call_next_param: str
    arg_resolvers: tuple[tuple[str, Any], ...]
    arg_factories: tuple[tuple[str, Any, Any | None], ...]
    return_shapes: tuple[Any, ...]

    # Optional root-level contribution (set by ``with_root``); auth
    # middlewares use this to register their securityScheme. Defaults to
    # a no-op.
    root_contribution: Callable[[spec.OpenAPI, SchemaRegistry], spec.OpenAPI] | None = field(default=None)

    def __call__(self, ctx: HTTPReqCtx, call_next: ApiOperation, /) -> OpResult:
        kwargs: dict[str, object] = {self.call_next_param: call_next}
        for name, resolver in self.arg_resolvers:
            value = resolver(ctx)
            if isinstance(value, OpResult):
                return value
            kwargs[name] = value
        result = self.target(**kwargs)
        if isinstance(result, OpResult):
            return result
        name = getattr(self.target, "__qualname__", None) or repr(self.target)
        raise TypeError(
            f"@op_middleware {name!r} returned {type(result).__name__}; "
            f"middlewares must return an OpResult (typically by calling call_next)"
        )

    def contribute_root(self, doc: spec.OpenAPI, registry: SchemaRegistry, /) -> spec.OpenAPI:
        if self.root_contribution is None:
            return doc
        return self.root_contribution(doc, registry)

    def contribute_operation(self, op: spec.Operation, registry: SchemaRegistry, /) -> spec.Operation:
        from localpost.openapi._operation_core import build_responses  # noqa: PLC0415

        # Parameters / requestBody come from the resolver factories.
        for _name, param, factory in self.arg_factories:
            if factory is None:
                continue
            op = factory.update_doc(param, op, registry)
        # Response codes come from the return-type union (sans the bare
        # ``OpResult`` passthrough sentinel). Don't overwrite codes the
        # operation already declared (the op's own response is more
        # specific than the middleware's generic one).
        for code, response in build_responses(self.return_shapes, registry).items():
            if code in op.responses:
                continue
            op = replace(op, responses={**op.responses, code: response})
        return op

    def with_root(self, contribution: Callable[[spec.OpenAPI, SchemaRegistry], spec.OpenAPI]) -> _FunctionMiddleware:
        """Return a copy that also contributes ``contribution`` at the root.

        Used by auth middlewares to register their ``SecurityScheme``
        while still inheriting the parameter / response contribution from
        the wrapped function's signature.
        """
        return replace(self, root_contribution=contribution)


def op_middleware(fn: Callable[..., Any]) -> _FunctionMiddleware:
    """Wrap ``fn`` as an :class:`OpMiddleware`.

    The wrapped function looks like an operation handler with one extra
    parameter — the ``call_next`` callable — which the framework passes
    in to let the middleware invoke the rest of the chain. Other
    parameters are resolved from the request (via :class:`FromHeader`,
    :class:`FromQuery`, :class:`FromBody`, …); the function must return
    an :class:`OpResult` — typically by ``return call_next(ctx)`` for
    the passthrough path, or a short-circuit result like
    ``Unauthorized(...)``.

    The ``call_next`` parameter is identified by name (``call_next``) or
    by annotation as :class:`ApiOperation` /
    ``Callable[[HTTPReqCtx], OpResult]``.

    The OpenAPI contribution is inferred from the same annotations: the
    declared parameters land on every operation that uses this
    middleware, and the response codes from the return-type union are
    added to those operations' ``responses`` (without overwriting codes
    the operation already declared). A bare ``OpResult`` member of the
    return union is the passthrough sentinel and contributes nothing.
    """
    from localpost.http import HTTPReqCtx  # noqa: PLC0415
    from localpost.openapi._operation_core import build_arg_resolvers, extract_response_shapes  # noqa: PLC0415

    call_next_param = _identify_call_next_param(fn)
    sig, runtime, factories = build_arg_resolvers(
        fn,
        exclude={call_next_param},
        ctx_types=(HTTPReqCtx,),
    )
    shapes_list, _null_is_not_found = extract_response_shapes(sig.return_annotation)
    return _FunctionMiddleware(
        target=fn,
        call_next_param=call_next_param,
        arg_resolvers=tuple(runtime),
        arg_factories=tuple(factories),
        return_shapes=tuple(shapes_list),
    )


def _identify_call_next_param(fn: Callable[..., Any]) -> str:
    """Pick the ``call_next`` parameter on ``fn``.

    Identification is by name (``call_next``); the parameter must exist
    and may be annotated as :class:`ApiOperation` /
    ``Callable[[HTTPReqCtx], OpResult]``. The annotation is informational
    — we don't require it.
    """
    sig = inspect.signature(fn)
    if "call_next" in sig.parameters:
        return "call_next"
    name = getattr(fn, "__qualname__", None) or getattr(fn, "__name__", repr(fn))
    raise ValueError(
        f"@op_middleware {name!r}: missing required 'call_next' parameter "
        f"(of type ApiOperation = Callable[[HTTPReqCtx], OpResult])"
    )
