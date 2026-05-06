"""Async per-operation wiring.

:class:`AsyncOperation` is the async sibling of
:class:`localpost.openapi.Operation`. The signature parsing,
return-type inference, and OpenAPI doc emission are shared via
:mod:`localpost.openapi._operation_core`; only the request-time
invocation differs — every step that touches the user fn or the wire
is awaited.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from http import HTTPMethod
from typing import Any, Self

from localpost.http._types import Response as _Response
from localpost.http.router import URITemplate
from localpost.openapi import spec as openapi_spec
from localpost.openapi._operation_core import (
    SSE_RESPONSE_HEADERS as _SSE_RESPONSE_HEADERS,
)
from localpost.openapi._operation_core import (
    ResponseShape,
    build_arg_resolvers,
    build_http_response,
    build_responses,
    extract_response_shapes,
    is_async_sse_payload,
    is_sync_iterable,
    iter_response_headers,
)
from localpost.openapi._operation_core import (
    operation_id as _make_operation_id,
)
from localpost.openapi._operation_core import (
    qualname as _qualname,
)
from localpost.openapi.adapters import AdapterRegistry, default_registry
from localpost.openapi.aio._ctx import AsyncHTTPReqCtx
from localpost.openapi.aio.middleware import AsyncApiOperation, AsyncOpMiddleware
from localpost.openapi.aio.sse import async_iter_events
from localpost.openapi.resolvers import (
    ArgResolver,
    ArgResolverFactory,
    FromPath,
)
from localpost.openapi.results import EventStreamResult, NotFound, Ok, OpResult
from localpost.openapi.schemas import SchemaRegistry

__all__ = ["AsyncOperation"]


@dataclass(frozen=True, slots=True)
class AsyncOperation:
    """Async sibling of :class:`localpost.openapi.Operation`.

    Same shape (method/path/template/target/resolvers/middlewares/return
    shapes) — only the runtime methods are async. The build-spec path is
    identical to the sync side and shares the type-level helpers.
    """

    method: HTTPMethod
    path: str
    template: URITemplate
    target: Callable[..., Any]
    """Async user fn — either a plain ``async def`` (returns a coroutine)
    or an ``async def`` generator (returns an async iterator for SSE).
    Both shapes are handled in :meth:`_run_core`."""
    arg_resolvers: tuple[tuple[str, ArgResolver], ...]
    arg_resolver_factories: tuple[tuple[str, inspect.Parameter, ArgResolverFactory | None], ...]
    middlewares: tuple[AsyncOpMiddleware, ...]
    return_shapes: tuple[ResponseShape, ...]
    null_is_not_found: bool
    summary: str
    operation_id: str
    description: str
    adapters: AdapterRegistry

    @classmethod
    def create(
        cls,
        method: HTTPMethod,
        path: str,
        fn: Callable[..., Any],
        /,
        *,
        middlewares: tuple[AsyncOpMiddleware, ...] = (),
        adapters: AdapterRegistry | None = None,
    ) -> Self:
        if not (inspect.iscoroutinefunction(fn) or inspect.isasyncgenfunction(fn)):
            raise TypeError(
                f"HttpAsyncApp handler {_qualname(fn)!r} must be ``async def`` (use HttpApp for sync handlers)"
            )

        template = URITemplate.parse(path)
        path_var_names = set(template.variable_names)
        registry = adapters or default_registry()

        sig, arg_resolvers, arg_factories = build_arg_resolvers(
            fn,
            path_var_names=path_var_names,
            adapters=registry,
            ctx_types=(AsyncHTTPReqCtx,),
        )

        # Validate path bindings: every {var} in the template must be
        # claimed by a parameter.
        bound_path_vars: set[str] = set()
        for _name, param, factory in arg_factories:
            if isinstance(factory, FromPath):
                bound = factory.name or param.name
                if bound not in path_var_names:
                    raise ValueError(
                        f"handler {_qualname(fn)!r}: parameter {param.name!r} uses FromPath"
                        f"({bound!r}) but path template {path!r} has no such variable"
                        f" (available: {sorted(path_var_names) or 'none'})"
                    )
                bound_path_vars.add(bound)
        unbound = path_var_names - bound_path_vars
        if unbound:
            raise ValueError(
                f"handler {_qualname(fn)!r}: path template {path!r} declares variable(s)"
                f" {sorted(unbound)!r} that no parameter resolves"
            )

        shapes_list, null_is_not_found = extract_response_shapes(sig.return_annotation)
        return_shapes = tuple(shapes_list)

        doc = inspect.getdoc(fn) or ""
        summary = doc.split("\n", 1)[0] if doc else f"{method.value} {path}"
        description = doc[len(summary) :].lstrip("\n") if doc else ""
        op_id = _make_operation_id(method, path, fn)

        return cls(
            method=method,
            path=path,
            template=template,
            target=fn,
            arg_resolvers=tuple(arg_resolvers),
            arg_resolver_factories=tuple(arg_factories),
            middlewares=middlewares,
            return_shapes=return_shapes,
            null_is_not_found=null_is_not_found,
            summary=summary,
            operation_id=op_id,
            description=description,
            adapters=registry,
        )

    # ----- runtime -----

    async def run(self, ctx: AsyncHTTPReqCtx) -> None:
        """Drive the full pipeline: middleware chain → arg resolvers →
        user fn → response build → ``ctx.complete`` / ``ctx.stream``."""
        chain: AsyncApiOperation = self._run_core
        for mw in reversed(self.middlewares):
            chain = _wrap_middleware(mw, chain)
        result = await chain(ctx)
        await self._write_response(ctx, result)

    async def _run_core(self, ctx: AsyncHTTPReqCtx) -> OpResult:
        kwargs: dict[str, object] = {}
        for name, resolver in self.arg_resolvers:
            value = resolver(ctx)
            if isinstance(value, OpResult):
                return value
            kwargs[name] = value
        result = self.target(**kwargs)
        # ``async def`` -> coroutine; ``async def`` with ``yield`` -> async iterator.
        if inspect.iscoroutine(result):
            result = await result
        if isinstance(result, OpResult):
            return result
        if is_sync_iterable(result):
            name = _qualname(self.target)
            raise TypeError(
                f"HttpAsyncApp handler {name!r} returned a sync iterator/generator; "
                f"use an ``async def`` generator (yields inside ``async def``) for SSE responses"
            )
        if is_async_sse_payload(result):
            return EventStreamResult(result)
        if result is None and self.null_is_not_found:
            return NotFound(None)
        return Ok(result)

    async def _write_response(self, ctx: AsyncHTTPReqCtx, result: OpResult) -> None:
        if isinstance(result, EventStreamResult):
            await _stream_sse(ctx, result, self.adapters)
            return
        response, body = build_http_response(result, self.adapters)
        await ctx.complete(response, body)

    # ----- spec build -----

    def build_spec(self, registry: SchemaRegistry) -> openapi_spec.Operation:
        op = openapi_spec.Operation(
            summary=self.summary,
            operation_id=self.operation_id,
            description=self.description,
        )
        for _name, param, factory in self.arg_resolver_factories:
            if factory is None:
                continue
            op = factory.update_doc(param, op, registry)
        responses = build_responses(self.return_shapes, registry)
        op = replace(op, responses=responses)
        for mw in self.middlewares:
            op = mw.contribute_operation(op, registry)
        return op


def _wrap_middleware(mw: AsyncOpMiddleware, call_next: AsyncApiOperation) -> AsyncApiOperation:
    async def wrapped(ctx: AsyncHTTPReqCtx) -> OpResult:
        return await mw(ctx, call_next)

    return wrapped


async def _stream_sse(
    ctx: AsyncHTTPReqCtx,
    result: EventStreamResult[Any],
    adapters: AdapterRegistry,
) -> None:
    """Run ``result``'s body as an SSE stream over an
    :class:`AsyncHTTPReqCtx`. Mirrors the sync ``_stream_sse``."""
    headers = list(_SSE_RESPONSE_HEADERS)
    seen = {name for name, _ in headers}
    for name, value in iter_response_headers(result.headers):
        if name.lower() in seen:
            continue
        headers.append((name, value))

    chunks: AsyncIterator[bytes] = async_iter_events(result.body, adapters)
    await ctx.stream(_Response(status_code=result.status_code, headers=headers), chunks)
