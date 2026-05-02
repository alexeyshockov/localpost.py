"""Argument resolvers — pull function parameters from the request.

Each resolver factory inspects an :class:`inspect.Parameter` once and
returns a runtime closure ``(HTTPReqCtx) -> object | OpResult``. Returning
an :class:`OpResult` short-circuits the operation (e.g. validation
failure) before the user function runs.

Resolvers also describe themselves to OpenAPI via :meth:`update_doc`
— this keeps the spec aligned with the runtime behaviour without a
separate declaration step.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from types import NoneType, UnionType
from typing import TYPE_CHECKING, Annotated, Any, Protocol, Union, get_args, get_origin
from urllib.parse import parse_qs

import msgspec

from localpost.http.router import RouteMatch
from localpost.openapi import spec as openapi_spec
from localpost.openapi.results import BadRequest, OpResult
from localpost.openapi.schemas import SchemaRegistry, is_pydantic_model

if TYPE_CHECKING:
    from localpost.http.server import HTTPReqCtx

__all__ = [
    "ArgResolver",
    "ArgResolverFactory",
    "FromPath",
    "FromQuery",
    "FromHeader",
    "FromBody",
    "is_body_type",
]


# Sentinel attached to ctx.attrs once we've parsed the query string for
# this request, so multiple FromQuery resolvers don't re-parse.
_QUERY_CACHE_KEY = "__lp_openapi_query__"


class ArgResolver(Protocol):
    def __call__(self, ctx: HTTPReqCtx, /) -> object | OpResult: ...


class ArgResolverFactory(Protocol):
    def __call__(self, param: inspect.Parameter, /) -> ArgResolver: ...

    def update_doc(
        self,
        param: inspect.Parameter,
        op: openapi_spec.Operation,
        registry: SchemaRegistry,
        /,
    ) -> openapi_spec.Operation: ...


# --- Helpers -------------------------------------------------------------


def _unwrap_annotated(t: Any) -> Any:
    """Strip ``Annotated[T, ...]`` to ``T``; return ``Any`` if no annotation."""
    if t is inspect.Parameter.empty:
        return Any
    if get_origin(t) is Annotated:
        return get_args(t)[0]
    return t


def _unwrap_optional(t: Any) -> tuple[Any, bool]:
    """Strip ``T | None`` / ``Optional[T]`` to ``(T, True)``.

    Returns ``(t, False)`` for non-optional types.
    """
    origin = get_origin(t)
    if origin is Union or origin is UnionType:
        args = [a for a in get_args(t) if a is not NoneType]
        if len(args) < len(get_args(t)):
            # ``T | None`` collapses to T; ``T | U | None`` keeps the union.
            inner: Any = args[0] if len(args) == 1 else Union[tuple(args)]  # noqa: UP007
            return inner, True
    return t, False


def _has_default(param: inspect.Parameter) -> bool:
    return param.default is not inspect.Parameter.empty


def _is_optional_param(param: inspect.Parameter) -> tuple[Any, bool, Any]:
    """For a param, return ``(target_type, is_optional, default_value)``.

    ``is_optional`` is true if the annotation is ``T | None`` *or* the param
    has a default. ``default_value`` is the param's default, or ``None`` when
    the type is optional but no default is supplied.
    """
    target = _unwrap_annotated(param.annotation)
    inner, is_nullable = _unwrap_optional(target)
    has_default = _has_default(param)
    if has_default:
        return inner, True, param.default
    if is_nullable:
        return inner, True, None
    return target, False, None


def _cast_str(value: str, target: Any) -> Any:
    """Coerce a single string value into ``target``.

    Falls back to :func:`msgspec.convert` with ``strict=False`` for the
    long tail (UUID, datetime, Decimal, Enum, …).
    """
    if target is str or target is Any or target is inspect.Parameter.empty:
        return value
    if target is int:
        return int(value)
    if target is float:
        return float(value)
    if target is bool:
        return value.lower() in ("true", "1", "yes", "on")
    if target is bytes:
        return value.encode()
    return msgspec.convert(value, type=target, strict=False)


def _list_element_type(t: Any) -> Any | None:
    """If ``t`` is ``list[X]`` / ``Sequence[X]`` / ``set[X]``, return ``X``."""
    origin = get_origin(t)
    if origin in (list, set, tuple, frozenset, Sequence):
        args = get_args(t)
        return args[0] if args else Any
    return None


def is_body_type(t: Any) -> bool:
    """True if ``t`` looks like something we should parse from the request body.

    Recognises :class:`msgspec.Struct` subclasses, dataclasses, ``TypedDict``,
    ``NamedTuple``, and pydantic models. Primitives stay as query params.
    """
    if not isinstance(t, type):
        return False
    if issubclass(t, msgspec.Struct):
        return True
    if hasattr(t, "__dataclass_fields__"):
        return True
    if hasattr(t, "__annotations__") and hasattr(t, "_fields"):  # NamedTuple
        return True
    return is_pydantic_model(t)


def _route_match(ctx: HTTPReqCtx) -> RouteMatch:
    return ctx.attrs[RouteMatch]


def _query_args(ctx: HTTPReqCtx) -> dict[str, list[str]]:
    cached = ctx.attrs.get(_QUERY_CACHE_KEY)
    if cached is not None:
        return cached
    qs = ctx.request.query_string.decode("iso-8859-1") if ctx.request.query_string else ""
    parsed: dict[str, list[str]] = parse_qs(qs, keep_blank_values=True) if qs else {}
    ctx.attrs[_QUERY_CACHE_KEY] = parsed
    return parsed


# --- FromPath ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FromPath:
    """Resolve a parameter from a URI template variable."""

    name: str | None = None
    description: str = ""

    def __call__(self, param: inspect.Parameter, /) -> ArgResolver:
        var_name = self.name or param.name
        target = _unwrap_annotated(param.annotation)

        def resolve(ctx: HTTPReqCtx) -> object | OpResult:
            raw = _route_match(ctx).path_args.get(var_name)
            if raw is None:
                return BadRequest(f"Missing path parameter: {var_name}")
            try:
                return _cast_str(raw, target)
            except (ValueError, msgspec.ValidationError) as exc:
                return BadRequest(f"Invalid path parameter {var_name!r}: {exc}")

        return resolve

    def update_doc(
        self,
        param: inspect.Parameter,
        op: openapi_spec.Operation,
        registry: SchemaRegistry,
        /,
    ) -> openapi_spec.Operation:
        target = _unwrap_annotated(param.annotation)
        schema = registry.schema_for(target) if target is not Any else {"type": "string"}
        parameter = openapi_spec.Parameter(
            name=self.name or param.name,
            location="path",
            required=True,
            description=self.description,
            schema=schema,
        )
        return replace(op, parameters=(*op.parameters, parameter))


# --- FromQuery -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FromQuery:
    """Resolve a parameter from the URL query string.

    For ``list[T]`` / ``Sequence[T]`` annotations all values for the key
    are returned. For scalar annotations the *first* value is taken and
    coerced.

    Optional handling: if the annotation is ``T | None`` *or* the param
    has a default, the parameter is not required. When absent it returns
    the default (``None`` if the type was simply made nullable).
    """

    name: str | None = None
    description: str = ""

    def __call__(self, param: inspect.Parameter, /) -> ArgResolver:
        param_name = self.name or param.name
        target, optional, default = _is_optional_param(param)
        elem_type = _list_element_type(target)
        is_list = elem_type is not None

        def resolve(ctx: HTTPReqCtx) -> object | OpResult:
            values = _query_args(ctx).get(param_name)
            if not values:
                if optional:
                    return default
                return BadRequest(f"Missing required query parameter: {param_name}")
            try:
                if is_list:
                    return [_cast_str(v, elem_type) for v in values]
                return _cast_str(values[0], target)
            except (ValueError, msgspec.ValidationError) as exc:
                return BadRequest(f"Invalid query parameter {param_name!r}: {exc}")

        return resolve

    def update_doc(
        self,
        param: inspect.Parameter,
        op: openapi_spec.Operation,
        registry: SchemaRegistry,
        /,
    ) -> openapi_spec.Operation:
        target, optional, _ = _is_optional_param(param)
        schema = registry.schema_for(target) if target is not Any else {"type": "string"}
        parameter = openapi_spec.Parameter(
            name=self.name or param.name,
            location="query",
            required=not optional,
            description=self.description,
            schema=schema,
        )
        return replace(op, parameters=(*op.parameters, parameter))


# --- FromHeader ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FromHeader:
    """Resolve a parameter from a request header.

    Optional handling: if the annotation is ``T | None`` *or* the param
    has a default, the header is not required.
    """

    name: str | None = None
    description: str = ""

    def __call__(self, param: inspect.Parameter, /) -> ArgResolver:
        header_name = (self.name or param.name.replace("_", "-")).lower().encode("ascii")
        target, optional, default = _is_optional_param(param)

        def resolve(ctx: HTTPReqCtx) -> object | OpResult:
            value: str | None = None
            for name, val in ctx.request.headers:
                if name == header_name:
                    value = val.decode("iso-8859-1")
                    break
            if value is None:
                if optional:
                    return default
                return BadRequest(f"Missing required header: {header_name.decode()}")
            try:
                return _cast_str(value, target)
            except (ValueError, msgspec.ValidationError) as exc:
                return BadRequest(f"Invalid header {header_name.decode()!r}: {exc}")

        return resolve

    def update_doc(
        self,
        param: inspect.Parameter,
        op: openapi_spec.Operation,
        registry: SchemaRegistry,
        /,
    ) -> openapi_spec.Operation:
        target, optional, _ = _is_optional_param(param)
        schema = registry.schema_for(target) if target is not Any else {"type": "string"}
        parameter = openapi_spec.Parameter(
            name=self.name or param.name.replace("_", "-"),
            location="header",
            required=not optional,
            description=self.description,
            schema=schema,
        )
        return replace(op, parameters=(*op.parameters, parameter))


# --- FromBody ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FromBody:
    """Resolve a parameter from the request body.

    Defaults to JSON parsing via :func:`msgspec.json.decode`. Raw ``bytes``
    / ``str`` annotations get the body verbatim. For pydantic models, uses
    :meth:`BaseModel.model_validate_json`.
    """

    content_type: str = "application/json"
    description: str = ""
    converter: Callable[[bytes, Any], object] | None = None

    def __call__(self, param: inspect.Parameter, /) -> ArgResolver:
        target = _unwrap_annotated(param.annotation)
        converter = self.converter or _default_body_converter(target)

        def resolve(ctx: HTTPReqCtx) -> object | OpResult:
            try:
                return converter(ctx.body, target)
            except msgspec.ValidationError as exc:
                return BadRequest(f"Invalid request body: {exc}")
            except (ValueError, TypeError) as exc:
                return BadRequest(f"Invalid request body: {exc}")

        return resolve

    def update_doc(
        self,
        param: inspect.Parameter,
        op: openapi_spec.Operation,
        registry: SchemaRegistry,
        /,
    ) -> openapi_spec.Operation:
        target = _unwrap_annotated(param.annotation)
        schema = registry.schema_for(target) if target is not Any else {}
        request_body = openapi_spec.RequestBody(
            description=self.description,
            required=not _has_default(param),
            content={self.content_type: openapi_spec.MediaType(schema=schema)},
        )
        return replace(op, request_body=request_body)


def _default_body_converter(target: Any) -> Callable[[bytes, Any], object]:
    if target is bytes:
        return _bytes_passthrough
    if target is str:
        return _str_decode
    if is_pydantic_model(target):
        return _pydantic_decode
    return _msgspec_decode


def _bytes_passthrough(body: bytes, _target: Any) -> object:
    return body


def _str_decode(body: bytes, _target: Any) -> object:
    return body.decode("utf-8")


def _msgspec_decode(body: bytes, target: Any) -> object:
    if not body:
        raise ValueError("empty request body")
    return msgspec.json.decode(body, type=target)


def _pydantic_decode(body: bytes, target: Any) -> object:
    if not body:
        raise ValueError("empty request body")
    return getattr(target, "model_validate_json")(body)  # noqa: B009
