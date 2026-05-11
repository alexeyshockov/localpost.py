"""Per-operation wiring: signature parsing, return-type inference, the
``(ctx) -> OpResult`` closure that runs at request time.

An :class:`Operation` is built once at decoration time and used in two
places: its :meth:`as_handler` populates the :class:`localpost.http.Routes`
table for dispatch, and its :meth:`build_spec` contributes a
:class:`localpost.openapi.spec.Operation` to the OpenAPI doc.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, replace
from http import HTTPMethod
from typing import Any, Self

from localpost.http import HTTPReqCtx, RequestHandler, read_body
from localpost.http._types import Response as _Response
from localpost.http.router import URITemplate
from localpost.openapi import spec as openapi_spec
from localpost.openapi._operation_core import SSE_RESPONSE_HEADERS as _SSE_RESPONSE_HEADERS
from localpost.openapi._operation_core import (
    ResponseShape,
    build_arg_resolvers,
    build_http_response,
    build_responses,
    extract_response_shapes,
    is_sse_payload,
    iter_response_headers,
)
from localpost.openapi._operation_core import (
    operation_id as _make_operation_id,
)
from localpost.openapi._operation_core import (
    qualname as _qualname,
)
from localpost.openapi.adapters import AdapterRegistry, default_registry
from localpost.openapi.middleware import ApiOperation, OpMiddleware
from localpost.openapi.resolvers import (
    BODY_CACHE_KEY,
    ArgResolver,
    ArgResolverFactory,
    FromBody,
    FromPath,
)
from localpost.openapi.results import EventStreamResult, NotFound, Ok, OpResult
from localpost.openapi.schemas import SchemaRegistry
from localpost.openapi.sse import iter_events

__all__ = [
    "Operation",
    "ResponseShape",
    "build_arg_resolvers",
    "extract_response_shapes",
]


@dataclass(frozen=True, slots=True)
class Operation:
    method: HTTPMethod
    path: str
    template: URITemplate
    target: Callable[..., Any]
    arg_resolvers: tuple[tuple[str, ArgResolver], ...]
    arg_resolver_factories: tuple[tuple[str, inspect.Parameter, ArgResolverFactory | None], ...]
    """Per-parameter ``(name, inspect.Parameter, factory)``. ``factory`` is
    ``None`` for the ``HTTPReqCtx`` pass-through, which contributes nothing to
    the OpenAPI doc."""

    middlewares: tuple[OpMiddleware, ...]
    """Middlewares wrapping the operation core. Includes both app-level
    middlewares (injected by :class:`HttpApp`) and per-operation middlewares.
    Outermost first; the chain is built in :meth:`_run` so each middleware's
    ``call_next`` invokes the next one."""

    return_shapes: tuple[ResponseShape, ...]
    null_is_not_found: bool
    """True when the return annotation was ``T | None`` and we therefore
    map a runtime ``None`` to a 404 instead of a 200 with empty body."""

    summary: str
    operation_id: str
    description: str

    adapters: AdapterRegistry
    """Type adapters used at request time for body decode and response
    encode. Threaded down from :class:`HttpApp`."""

    needs_body: bool
    """``True`` iff at least one resolved parameter is a :class:`FromBody`
    factory. The runtime pre-buffers the request body via
    :func:`localpost.http.read_body` and stashes it in
    ``ctx.attrs[BODY_CACHE_KEY]`` before invoking resolvers."""

    @classmethod
    def create(
        cls,
        method: HTTPMethod,
        path: str,
        fn: Callable[..., Any],
        /,
        *,
        middlewares: tuple[OpMiddleware, ...] = (),
        adapters: AdapterRegistry | None = None,
    ) -> Self:
        template = URITemplate.parse(path)
        path_var_names = set(template.variable_names)
        registry = adapters or default_registry()

        sig, arg_resolvers, arg_factories = build_arg_resolvers(
            fn,
            path_var_names=path_var_names,
            adapters=registry,
            ctx_types=(HTTPReqCtx,),
        )

        # Validate path bindings: every {var} in the template must be
        # claimed by a parameter (whether implicitly via name match or
        # explicitly via Annotated[..., FromPath("var")]).
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

        needs_body = any(isinstance(factory, FromBody) for _, _, factory in arg_factories)

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
            needs_body=needs_body,
        )

    # ----- runtime -----

    def as_handler(self) -> RequestHandler:
        """Build the ``RequestHandler`` registered with the router.

        Runs the full operation pipeline in one shot: middleware chain →
        arg resolvers → user fn → response build → ``ctx.complete``.
        Operations that consume the request body (parameters with a
        :class:`FromBody` factory) call :func:`localpost.http.read_body`
        from inside :meth:`_run_core` — the conn must therefore be on a
        worker thread (composed via
        :func:`localpost.http.thread_pool_handler`) before this handler
        runs.
        """
        return self._run

    def _run(self, ctx: HTTPReqCtx) -> None:
        chain: ApiOperation = self._run_core
        for mw in reversed(self.middlewares):
            chain = _wrap_middleware(mw, chain)
        result = chain(ctx)
        self._write_response(ctx, result)

    def _run_core(self, ctx: HTTPReqCtx) -> OpResult:
        """Resolve args, invoke the target, normalise the return value to
        an :class:`OpResult`. SSE-shaped returns (generator, iterator,
        :class:`EventStream`) are wrapped in :class:`EventStreamResult`
        so middleware can post-process / wrap the stream uniformly."""
        if self.needs_body and BODY_CACHE_KEY not in ctx.attrs:
            ctx.attrs[BODY_CACHE_KEY] = read_body(ctx)
        kwargs: dict[str, object] = {}
        for name, resolver in self.arg_resolvers:
            value = resolver(ctx)
            if isinstance(value, OpResult):
                return value
            kwargs[name] = value
        result = self.target(**kwargs)
        if isinstance(result, OpResult):
            return result
        if is_sse_payload(result):
            return EventStreamResult(result)
        if result is None and self.null_is_not_found:
            # ``T | None`` returning None → implicit 404.
            return NotFound(None)
        return Ok(result)

    def _write_response(self, ctx: HTTPReqCtx, result: OpResult) -> None:
        if isinstance(result, EventStreamResult):
            _stream_sse(ctx, result, self.adapters)
            return
        response, body = build_http_response(result, self.adapters)
        ctx.complete(response, body)

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


# --- SSE streaming -------------------------------------------------------


def _wrap_middleware(mw: OpMiddleware, call_next: ApiOperation) -> ApiOperation:
    def wrapped(ctx: HTTPReqCtx) -> OpResult:
        return mw(ctx, call_next)

    return wrapped


def _stream_sse(ctx: HTTPReqCtx, result: EventStreamResult[Any], adapters: AdapterRegistry) -> None:
    """Run ``result``'s body as an SSE stream over ``ctx``.

    Builds the SSE response (chunked transfer encoding is implicit — we
    don't set ``content-length`` so h11 picks chunked) and hands the
    event iterator to :meth:`HTTPReqCtx.stream`. The transport owns
    iteration: the native server interleaves cancellation checks per
    chunk, the WSGI bridge hands the iterator straight to the WSGI
    server. Any user-set headers on ``result`` are merged in (without
    overriding the framework-set ``content-type`` / ``cache-control`` /
    ``x-accel-buffering``).
    """
    headers = list(_SSE_RESPONSE_HEADERS)
    seen = {name for name, _ in headers}
    for name, value in iter_response_headers(result.headers):
        if name.lower() in seen:
            continue
        headers.append((name, value))

    ctx.stream(
        _Response(status_code=result.status_code, headers=headers),
        iter_events(result.body, adapters),
    )
