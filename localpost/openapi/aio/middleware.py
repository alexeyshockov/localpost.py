"""``AsyncOpMiddleware`` — async sibling of :class:`OpMiddleware`.

Same idea as the sync version: middleware wraps an operation core,
receives the request ctx + a ``call_next`` callable, returns an
:class:`OpResult`. The async flavour awaits ``call_next``, the user
function, and any user-side I/O.

The OpenAPI doc-contribution surface (:meth:`contribute_root` /
:meth:`contribute_operation`) is identical to the sync protocol — those
methods build static spec documents and don't touch the event loop.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from localpost.openapi.results import OpResult

if TYPE_CHECKING:
    from localpost.openapi import spec
    from localpost.openapi.aio._ctx import AsyncHTTPReqCtx
    from localpost.openapi.schemas import SchemaRegistry

__all__ = ["AsyncApiOperation", "AsyncOpMiddleware", "async_op_middleware"]


type AsyncApiOperation = Callable[["AsyncHTTPReqCtx"], Awaitable["OpResult"]]
"""The shape of an async operation as seen by middleware.

A middleware's ``call_next`` is an :class:`AsyncApiOperation` — given the
request context, it returns an awaitable :class:`OpResult` (which may be
an :class:`~localpost.openapi.results.EventStreamResult` for SSE handlers).
"""


@runtime_checkable
class AsyncOpMiddleware(Protocol):
    """Async operation middleware that knows how to describe itself in OpenAPI.

    Differences from :class:`localpost.openapi.OpMiddleware`:

    - :meth:`__call__` is ``async`` and awaits ``call_next``.
    - The ``ctx`` parameter is an :class:`AsyncHTTPReqCtx` rather than the
      sync :class:`HTTPReqCtx`.
    """

    async def __call__(self, ctx: AsyncHTTPReqCtx, call_next: AsyncApiOperation, /) -> OpResult:
        """Run around the operation. Return an :class:`OpResult` —
        typically by awaiting ``call_next(ctx)`` and forwarding (or
        post-processing) its result, or by short-circuiting with a
        middleware-specific result (e.g. ``Unauthorized``)."""
        ...

    def contribute_root(self, doc: spec.OpenAPI, registry: SchemaRegistry, /) -> spec.OpenAPI:
        """App-level OpenAPI contribution. Default: return ``doc`` unchanged."""
        ...

    def contribute_operation(self, op: spec.Operation, registry: SchemaRegistry, /) -> spec.Operation:
        """Per-operation OpenAPI contribution. Default: return ``op`` unchanged."""
        ...


@dataclass(eq=False, slots=True)
class _AsyncFunctionMiddleware:
    """Concrete :class:`AsyncOpMiddleware` produced by :func:`async_op_middleware`.

    Same shape as the sync :class:`_FunctionMiddleware`, but the wrapped
    function is awaited and the OpenAPI contribution closure is identical
    (it builds spec data, no I/O).
    """

    target: Callable[..., Awaitable[Any]]
    call_next_param: str
    arg_resolvers: tuple[tuple[str, Any], ...]
    arg_factories: tuple[tuple[str, Any, Any | None], ...]
    return_shapes: tuple[Any, ...]

    root_contribution: Callable[[spec.OpenAPI, SchemaRegistry], spec.OpenAPI] | None = field(default=None)

    async def __call__(self, ctx: AsyncHTTPReqCtx, call_next: AsyncApiOperation, /) -> OpResult:
        kwargs: dict[str, object] = {self.call_next_param: call_next}
        for name, resolver in self.arg_resolvers:
            value = resolver(ctx)
            if isinstance(value, OpResult):
                return value
            kwargs[name] = value
        result = await self.target(**kwargs)
        if isinstance(result, OpResult):
            return result
        name = getattr(self.target, "__qualname__", None) or repr(self.target)
        raise TypeError(
            f"@async_op_middleware {name!r} returned {type(result).__name__}; "
            f"middlewares must return an OpResult (typically by awaiting call_next)"
        )

    def contribute_root(self, doc: spec.OpenAPI, registry: SchemaRegistry, /) -> spec.OpenAPI:
        if self.root_contribution is None:
            return doc
        return self.root_contribution(doc, registry)

    def contribute_operation(self, op: spec.Operation, registry: SchemaRegistry, /) -> spec.Operation:
        from localpost.openapi._operation_core import build_responses

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

    def with_root(
        self, contribution: Callable[[spec.OpenAPI, SchemaRegistry], spec.OpenAPI]
    ) -> _AsyncFunctionMiddleware:
        """Return a copy that also contributes ``contribution`` at the root.

        Used by async auth middlewares to register their ``SecurityScheme``
        while still inheriting the parameter / response contribution from
        the wrapped function's signature.
        """
        return replace(self, root_contribution=contribution)


def async_op_middleware(fn: Callable[..., Awaitable[Any]]) -> _AsyncFunctionMiddleware:
    """Wrap an ``async def`` function as an :class:`AsyncOpMiddleware`.

    The wrapped function looks like an async operation handler with one
    extra parameter — the ``call_next`` callable — which the framework
    passes in to let the middleware invoke the rest of the chain. Other
    parameters are resolved from the request (via :class:`FromHeader`,
    :class:`FromQuery`, :class:`FromBody`, …); the function must return
    an :class:`OpResult` — typically by ``return await call_next(ctx)``
    for the passthrough path, or a short-circuit result like
    ``Unauthorized(...)``.

    The OpenAPI contribution mirrors the sync :func:`op_middleware`: the
    declared parameters land on every operation that uses this middleware,
    and the response codes from the return-type union are added to those
    operations' ``responses`` (without overwriting codes the operation
    already declared). A bare ``OpResult`` member of the return union is
    the passthrough sentinel and contributes nothing.
    """
    if not inspect.iscoroutinefunction(fn):
        name = getattr(fn, "__qualname__", None) or getattr(fn, "__name__", repr(fn))
        raise TypeError(
            f"@async_op_middleware {name!r}: function must be an ``async def`` "
            f"(use @op_middleware for sync middlewares)"
        )

    from localpost.openapi._operation_core import build_arg_resolvers, extract_response_shapes
    from localpost.openapi.aio._ctx import AsyncHTTPReqCtx as _AsyncCtx

    call_next_param = _identify_call_next_param(fn)
    sig, runtime, factories = build_arg_resolvers(
        fn,
        exclude={call_next_param},
        ctx_types=(_AsyncCtx,),
    )
    shapes_list, _null_is_not_found = extract_response_shapes(sig.return_annotation)
    return _AsyncFunctionMiddleware(
        target=fn,
        call_next_param=call_next_param,
        arg_resolvers=tuple(runtime),
        arg_factories=tuple(factories),
        return_shapes=tuple(shapes_list),
    )


def _identify_call_next_param(fn: Callable[..., Any]) -> str:
    """Pick the ``call_next`` parameter on ``fn``. Identification is by name."""
    sig = inspect.signature(fn)
    if "call_next" in sig.parameters:
        return "call_next"
    name = getattr(fn, "__qualname__", None) or getattr(fn, "__name__", repr(fn))
    raise ValueError(
        f"@async_op_middleware {name!r}: missing required 'call_next' parameter "
        f"(of type AsyncApiOperation = Callable[[AsyncHTTPReqCtx], Awaitable[OpResult]])"
    )
