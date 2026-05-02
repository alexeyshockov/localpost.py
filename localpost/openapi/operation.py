"""Per-operation wiring: signature parsing, return-type inference, the
``(ctx) -> Response`` closure that runs at request time.

An :class:`Operation` is built once at decoration time and used in two
places: its :meth:`as_handler` populates the :class:`localpost.http.Routes`
table for dispatch, and its :meth:`build_spec` contributes a
:class:`localpost.openapi.spec.Operation` to the OpenAPI doc.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from http import HTTPMethod
from types import UnionType
from typing import Annotated, Any, Self, Union, get_args, get_origin, get_type_hints

import msgspec

from localpost.http._types import Response as _Response
from localpost.http.router import URITemplate
from localpost.http.server import BodyHandler, HTTPReqCtx, RequestHandler
from localpost.openapi import spec as openapi_spec
from localpost.openapi.filter import OpFilter
from localpost.openapi.resolvers import (
    ArgResolver,
    ArgResolverFactory,
    FromBody,
    FromPath,
    FromQuery,
    is_body_type,
)
from localpost.openapi.results import NoContent, Ok, OpResult
from localpost.openapi.schemas import SchemaRegistry, is_pydantic_model

__all__ = ["Operation"]


# Sentinel — we expose the request ctx for params explicitly typed as HTTPReqCtx.
def _resolve_ctx(ctx: HTTPReqCtx) -> HTTPReqCtx:
    return ctx


@dataclass(frozen=True, slots=True)
class _ResponseShape:
    """One branch of the return type — a (status, body type) pair."""

    status_code: int
    description: str
    body_type: Any | None


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

    filters: tuple[OpFilter, ...]
    """Filters to run before the resolvers. Includes both app-level filters
    (injected by :class:`HttpApp`) and per-operation filters."""

    return_shapes: tuple[_ResponseShape, ...]
    summary: str
    operation_id: str
    description: str

    @classmethod
    def create(
        cls,
        method: HTTPMethod,
        path: str,
        fn: Callable[..., Any],
        /,
        *,
        filters: tuple[OpFilter, ...] = (),
    ) -> Self:
        template = URITemplate.parse(path)
        path_var_names = set(template.variable_names)

        # Resolve PEP 563 string annotations (``from __future__ import
        # annotations`` is in effect for most callers) into the real types
        # before feeding them to the resolver factories. We build a localns
        # from the function's closure cells so types defined in an enclosing
        # scope (common in tests, factories) resolve too.
        localns = _closure_locals(fn)
        try:
            sig = inspect.signature(fn, eval_str=True, locals=localns)
        except Exception:  # noqa: BLE001
            sig = inspect.signature(fn)
        try:
            hints = get_type_hints(fn, localns=localns, include_extras=True)
        except Exception:  # noqa: BLE001
            hints = {}
        sig = _signature_with_hints(sig, hints)

        arg_resolvers: list[tuple[str, ArgResolver]] = []
        arg_factories: list[tuple[str, inspect.Parameter, ArgResolverFactory | None]] = []

        for name, param in sig.parameters.items():
            if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                raise ValueError(f"handler {_qualname(fn)!r}: *args / **kwargs not supported")
            factory = _pick_factory(name, param, path_var_names)
            if factory is None:
                # HTTPReqCtx pass-through.
                arg_resolvers.append((name, _resolve_ctx))
                arg_factories.append((name, param, None))
            else:
                arg_resolvers.append((name, factory(param)))
                arg_factories.append((name, param, factory))

        return_shapes = tuple(_extract_response_shapes(sig.return_annotation))

        doc = inspect.getdoc(fn) or ""
        summary = doc.split("\n", 1)[0] if doc else f"{method.value} {path}"
        description = doc[len(summary) :].lstrip("\n") if doc else ""
        operation_id = f"{method.value.lower()}_{_path_to_id(path)}"

        return cls(
            method=method,
            path=path,
            template=template,
            target=fn,
            arg_resolvers=tuple(arg_resolvers),
            arg_resolver_factories=tuple(arg_factories),
            filters=filters,
            return_shapes=return_shapes,
            summary=summary,
            operation_id=operation_id,
            description=description,
        )

    # ----- runtime -----

    def as_handler(self) -> RequestHandler:
        """Build the ``RequestHandler`` registered with the router.

        The pre-body phase returns a ``BodyHandler`` so that
        :func:`localpost.http.thread_pool_handler` offloads execution to a
        worker after the request body is buffered. The body handler runs the
        full operation pipeline: arg resolvers → user fn → response build →
        ``ctx.complete``.
        """
        run = self._run

        def pre_body(_ctx: HTTPReqCtx) -> BodyHandler:
            return run

        return pre_body

    def _run(self, ctx: HTTPReqCtx) -> None:
        for f in self.filters:
            short_circuit = f(ctx)
            if short_circuit is not None:
                response, body = _build_http_response(short_circuit)
                ctx.complete(response, body)
                return
        kwargs: dict[str, object] = {}
        for name, resolver in self.arg_resolvers:
            value = resolver(ctx)
            if isinstance(value, OpResult):
                response, body = _build_http_response(value)
                ctx.complete(response, body)
                return
            kwargs[name] = value
        result = self.target(**kwargs)
        if isinstance(result, _Response):
            ctx.complete(result, b"")
            return
        if isinstance(result, OpResult):
            op_result: OpResult = result
        else:
            op_result = Ok(result)
        response, body = _build_http_response(op_result)
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
        responses = _build_responses(self.return_shapes, registry)
        op = replace(op, responses=responses)
        for f in self.filters:
            op = f.contribute_operation(op, registry)
        return op


# --- Helpers -------------------------------------------------------------


def _qualname(fn: Callable[..., Any]) -> str:
    return getattr(fn, "__qualname__", None) or getattr(fn, "__name__", repr(fn))


def _closure_locals(fn: Callable[..., Any]) -> dict[str, Any]:
    """Return a name → value dict from ``fn``'s closure cells.

    Used as ``localns`` for type-annotation resolution so types defined in
    an enclosing function scope are visible (common in tests, model
    factories).
    """
    closure = getattr(fn, "__closure__", None) or ()
    code = getattr(fn, "__code__", None)
    if not closure or code is None:
        return {}
    free_vars = code.co_freevars
    locals_dict: dict[str, Any] = {}
    for name, cell in zip(free_vars, closure, strict=False):
        try:
            locals_dict[name] = cell.cell_contents
        except ValueError:
            continue
    return locals_dict


def _signature_with_hints(sig: inspect.Signature, hints: dict[str, Any]) -> inspect.Signature:
    """Return ``sig`` with each parameter's ``annotation`` replaced by the
    resolved type from ``hints``. Same for the return annotation.

    With ``from __future__ import annotations`` in effect, ``param.annotation``
    is a ``str``; we need the real type for runtime introspection.
    """
    new_params = [
        param.replace(annotation=hints[name]) if name in hints else param for name, param in sig.parameters.items()
    ]
    return_annotation = hints.get("return", sig.return_annotation)
    return sig.replace(parameters=new_params, return_annotation=return_annotation)


def _path_to_id(path: str) -> str:
    cleaned = path.replace("/", "_").replace("{", "").replace("}", "").strip("_")
    return cleaned or "root"


def _pick_factory(
    name: str,
    param: inspect.Parameter,
    path_var_names: set[str],
) -> ArgResolverFactory | None:
    """Return the resolver factory for one parameter, or ``None`` for the
    ``HTTPReqCtx`` pass-through."""
    annotation = param.annotation

    # Explicit ``Annotated[T, FromX(...)]`` wins.
    if get_origin(annotation) is Annotated:
        for arg in get_args(annotation)[1:]:
            if _is_resolver_factory(arg):
                return arg

    target = annotation
    if get_origin(target) is Annotated:
        target = get_args(target)[0]

    if name in path_var_names:
        return FromPath(name=name)
    if target is HTTPReqCtx:
        return None
    if is_body_type(target):
        return FromBody()
    return FromQuery(name=name)


def _is_resolver_factory(arg: Any) -> ArgResolverFactory | None:
    """Duck-typing for our :class:`ArgResolverFactory` ``Protocol``.

    We can't ``isinstance``-check it because the protocol is structural and
    not ``@runtime_checkable``; resolver factories are tagged by carrying an
    ``update_doc`` method on top of being callable.
    """
    if callable(arg) and hasattr(arg, "update_doc"):
        return arg
    return None


def _extract_response_shapes(return_annotation: Any) -> list[_ResponseShape]:
    if return_annotation is inspect.Signature.empty:
        return [_ResponseShape(200, "Successful response", None)]

    origin = get_origin(return_annotation)
    members = list(get_args(return_annotation)) if origin is Union or origin is UnionType else [return_annotation]

    shapes: list[_ResponseShape] = []
    seen_codes: set[int] = set()
    has_success = False
    for member in members:
        if member is type(None):
            continue
        member_origin = get_origin(member)
        cls = member_origin if member_origin is not None else member
        if isinstance(cls, type) and issubclass(cls, OpResult):
            code = cls._status_code
            description = cls._description
            body_type: Any | None
            if cls is NoContent:
                body_type = None
            else:
                generic_args = get_args(member) if member_origin is not None else ()
                body_type = generic_args[0] if generic_args else None
            if code < 400:
                has_success = True
        elif isinstance(cls, type) and issubclass(cls, _Response):
            # Bare ``Response`` escape hatch — emit nothing for this branch.
            has_success = True
            continue
        else:
            code = 200
            description = "Successful response"
            body_type = member
            has_success = True
        if code in seen_codes:
            continue
        seen_codes.add(code)
        shapes.append(_ResponseShape(code, description, body_type))

    if not has_success and not shapes:
        shapes.append(_ResponseShape(200, "Successful response", None))
    return shapes


def _build_responses(shapes: tuple[_ResponseShape, ...], registry: SchemaRegistry) -> dict[str, openapi_spec.Response]:
    responses: dict[str, openapi_spec.Response] = {}
    for shape in shapes:
        if shape.body_type is None:
            responses[str(shape.status_code)] = openapi_spec.Response(description=shape.description)
            continue
        schema = registry.schema_for(shape.body_type)
        responses[str(shape.status_code)] = openapi_spec.Response(
            description=shape.description,
            content={"application/json": openapi_spec.MediaType(schema=schema)},
        )
    return responses


# --- Response building ---------------------------------------------------

_JSON_CONTENT_TYPE = b"application/json"
_TEXT_CONTENT_TYPE = b"text/plain; charset=utf-8"
_OCTET_CONTENT_TYPE = b"application/octet-stream"


def _encode_body(value: object) -> tuple[bytes, bytes]:
    """Return ``(body_bytes, content_type)`` for ``value``.

    Pass-through for ``bytes`` / ``str``; pydantic models go through
    ``model_dump_json``; everything else through :func:`msgspec.json.encode`.
    """
    if value is None:
        return b"", b""
    if isinstance(value, bytes):
        return value, _OCTET_CONTENT_TYPE
    if isinstance(value, bytearray):
        return bytes(value), _OCTET_CONTENT_TYPE
    if isinstance(value, str):
        return value.encode("utf-8"), _TEXT_CONTENT_TYPE
    if is_pydantic_model(type(value)):
        return getattr(value, "model_dump_json")().encode("utf-8"), _JSON_CONTENT_TYPE  # noqa: B009
    return msgspec.json.encode(value), _JSON_CONTENT_TYPE


def _build_http_response(result: OpResult) -> tuple[_Response, bytes]:
    body_bytes, default_ct = _encode_body(result.body)
    headers: list[tuple[bytes, bytes]] = []
    headers_seen: set[bytes] = set()
    for name, value in _iter_headers(result.headers):
        headers.append((name, value))
        headers_seen.add(name.lower())
    if body_bytes and b"content-type" not in headers_seen and default_ct:
        headers.append((b"content-type", default_ct))
    if b"content-length" not in headers_seen:
        headers.append((b"content-length", str(len(body_bytes)).encode("ascii")))
    return _Response(status_code=result.status_code, headers=headers), body_bytes


def _iter_headers(headers: Mapping[str, str]):
    for name, value in headers.items():
        yield name.encode("ascii"), value.encode("iso-8859-1")
