"""Type-level core shared by sync :class:`Operation` and async :class:`AsyncOperation`.

Everything here is independent of the runtime invocation style — signature
parsing, return-type inference, OpenAPI doc contribution helpers, response
encoding. The runtime closures (``_run`` / ``_run_core`` / ``_write_response``
/ ``_stream_sse``) live in the per-flavour ``operation.py`` files.
"""

from __future__ import annotations

import inspect
from collections.abc import (
    AsyncGenerator,
    AsyncIterable,
    AsyncIterator,
    Callable,
    Generator,
    Iterable,
    Iterator,
    Mapping,
)
from dataclasses import dataclass, replace
from http import HTTPMethod
from types import UnionType
from typing import Annotated, Any, Union, cast, get_args, get_origin, get_type_hints

from localpost.http._types import Response as _Response
from localpost.openapi import spec as openapi_spec
from localpost.openapi.adapters import AdapterRegistry, default_registry
from localpost.openapi.resolvers import (
    ArgResolver,
    ArgResolverFactory,
    FromBody,
    FromPath,
    FromQuery,
    is_body_type,
)
from localpost.openapi.results import EventStreamResult, NoContent, OpResult
from localpost.openapi.schemas import SchemaRegistry
from localpost.openapi.sse import EventStream

__all__ = [
    "ResponseShape",
    "build_arg_resolvers",
    "extract_response_shapes",
    "build_responses",
    "build_http_response",
    "encode_body",
    "iter_response_headers",
    "qualname",
    "operation_id",
    "is_sse_payload",
    "is_async_sse_payload",
    "is_sync_iterable",
    "SSE_RESPONSE_HEADERS",
    "resolve_ctx",
]


# Sentinel — handed back as an :class:`ArgResolver` for parameters explicitly
# typed as :class:`HTTPReqCtx` (or its async sibling). The actual ctx object
# is passed in at request time, sync and async runtimes share the closure.
def resolve_ctx(ctx: Any) -> Any:
    return ctx


@dataclass(frozen=True, slots=True)
class ResponseShape:
    """One branch of the return type — a (status, body type, content type)."""

    status_code: int
    description: str
    body_type: Any | None
    content_type: str = "application/json"


# --- Signature parsing ---------------------------------------------------


def qualname(fn: Callable[..., Any]) -> str:
    return getattr(fn, "__qualname__", None) or getattr(fn, "__name__", repr(fn))


def build_arg_resolvers(
    fn: Callable[..., Any],
    *,
    path_var_names: set[str] | None = None,
    adapters: AdapterRegistry | None = None,
    exclude: set[str] | None = None,
    ctx_types: tuple[type, ...] = (),
) -> tuple[
    inspect.Signature,
    list[tuple[str, ArgResolver]],
    list[tuple[str, inspect.Parameter, ArgResolverFactory | None]],
]:
    """Inspect ``fn`` and return ``(resolved_signature, runtime_resolvers,
    spec_factories)`` for use by sync :class:`Operation`, async
    :class:`AsyncOperation`, or the ``op_middleware`` decorators.

    Path-template binding is *not* validated here — pass ``path_var_names``
    so :class:`FromPath` is auto-picked for matching parameter names; the
    caller is responsible for any "unbound var" error.

    ``adapters`` flows into auto-picked / un-bound :class:`FromBody`
    factories so body decoding hits the per-app type adapter registry. If
    ``None``, falls back to :func:`default_registry`.

    ``exclude`` skips parameters by name — used by ``op_middleware`` to drop
    the ``call_next`` parameter from resolver inspection.

    ``ctx_types`` lists the request-context types that should be treated
    as the framework pass-through (no factory, no doc contribution). Sync
    callers pass ``(HTTPReqCtx,)``; async callers pass
    ``(AsyncHTTPReqCtx,)``. Defaults to ``()`` so a parameter must opt in
    to the pass-through via the caller-supplied tuple.
    """
    path_vars = path_var_names or set()
    registry = adapters or default_registry()
    skip = exclude or set()

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

    runtime: list[tuple[str, ArgResolver]] = []
    factories: list[tuple[str, inspect.Parameter, ArgResolverFactory | None]] = []
    for name, param in sig.parameters.items():
        if name in skip:
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            raise ValueError(f"handler {qualname(fn)!r}: *args / **kwargs not supported")
        factory = _pick_factory(name, param, path_vars, registry, ctx_types)
        if factory is None:
            # Request ctx pass-through.
            runtime.append((name, resolve_ctx))
            factories.append((name, param, None))
        else:
            # Inject the registry into FromBody so its closure binds the
            # right adapter at build time. User-supplied factories are left
            # alone — they're trusted to handle their own decoding.
            if isinstance(factory, FromBody) and factory.adapters is None:
                factory = replace(factory, adapters=registry)
            runtime.append((name, factory(param)))
            factories.append((name, param, factory))
    return sig, runtime, factories


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
    resolved type from ``hints``. Same for the return annotation."""
    new_params = [
        param.replace(annotation=hints[name]) if name in hints else param for name, param in sig.parameters.items()
    ]
    return_annotation = hints.get("return", sig.return_annotation)
    return sig.replace(parameters=new_params, return_annotation=return_annotation)


def _path_to_id(path: str) -> str:
    cleaned = path.replace("/", "_").replace("{", "").replace("}", "").strip("_")
    return cleaned or "root"


def operation_id(method: HTTPMethod, path: str, fn: Callable[..., Any]) -> str:
    """Pick an operationId.

    Prefer ``fn.__name__`` when it's a real, non-anonymous identifier — it
    matches what users see in their codebase (and what FastAPI emits, modulo
    the path suffix). Fall back to a path-mangled id for lambdas / wrapped
    callables / anything without a usable name.
    """
    name = getattr(fn, "__name__", "") or ""
    if name and name not in {"<lambda>", "_", ""}:
        return name
    return f"{method.value.lower()}_{_path_to_id(path)}"


def _pick_factory(
    name: str,
    param: inspect.Parameter,
    path_var_names: set[str],
    adapters: AdapterRegistry,
    ctx_types: tuple[type, ...],
) -> ArgResolverFactory | None:
    """Return the resolver factory for one parameter, or ``None`` for the
    request-ctx pass-through."""
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
    if ctx_types and target in ctx_types:
        return None
    if is_body_type(target, adapters):
        return FromBody()
    return FromQuery(name=name)


def _is_resolver_factory(arg: Any) -> ArgResolverFactory | None:
    """Duck-typing for our :class:`ArgResolverFactory` ``Protocol``.

    We can't ``isinstance``-check it because the protocol is structural and
    not ``@runtime_checkable``; resolver factories are tagged by carrying an
    ``update_doc`` method on top of being callable.
    """
    if callable(arg) and hasattr(arg, "update_doc"):
        return cast(ArgResolverFactory, arg)
    return None


# --- Return-type → response-shape extraction ----------------------------


def extract_response_shapes(return_annotation: Any) -> tuple[list[ResponseShape], bool]:
    """Return ``(shapes, null_is_not_found)`` from a function's return annotation."""
    if return_annotation is inspect.Signature.empty:
        return [ResponseShape(200, "Successful response", None)], False

    origin = get_origin(return_annotation)
    members = list(get_args(return_annotation)) if origin is Union or origin is UnionType else [return_annotation]

    shapes: list[ResponseShape] = []
    seen_codes: set[int] = set()
    has_success = False
    has_none = False
    for member in members:
        if member is type(None):
            has_none = True
            continue
        member_origin = get_origin(member)
        cls = member_origin if member_origin is not None else member
        sse_payload = _sse_payload_type(member)
        if sse_payload is not _NOT_SSE:
            code = 200
            description = "Successful response"
            body_type = sse_payload
            content_type = "text/event-stream"
            has_success = True
        elif isinstance(cls, type) and issubclass(cls, EventStreamResult):
            code = cls._status_code
            description = cls._description
            generic_args = get_args(member) if member_origin is not None else ()
            body_type = generic_args[0] if generic_args else None
            content_type = "text/event-stream"
            has_success = True
        elif isinstance(cls, type) and issubclass(cls, OpResult):
            code = cls._status_code
            description = cls._description
            body_type = None
            content_type = "application/json"
            if cls is not NoContent:
                generic_args = get_args(member) if member_origin is not None else ()
                body_type = generic_args[0] if generic_args else None
            if code < 400:
                has_success = True
        else:
            code = 200
            description = "Successful response"
            body_type = member
            content_type = "application/json"
            has_success = True
        if code in seen_codes:
            continue
        seen_codes.add(code)
        shapes.append(ResponseShape(code, description, body_type, content_type))

    # ``T | None`` is the implicit "T or 404 NotFound" — only when paired
    # with at least one non-None success type (a bare ``-> None`` annotation
    # still means "204 / empty success", not 404), and only when the user
    # didn't already declare 404 explicitly via ``NotFound[X]``.
    from localpost.openapi.results import NotFound as _NotFound

    null_is_not_found = has_none and has_success and 404 not in seen_codes
    if null_is_not_found:
        shapes.append(ResponseShape(_NotFound._status_code, _NotFound._description, None))
        seen_codes.add(404)

    if not has_success and not shapes:
        shapes.append(ResponseShape(200, "Successful response", None))
    return shapes, null_is_not_found


# Sentinel: unique object so callers can distinguish "not an SSE return"
# from "SSE return with no payload type" (e.g. a bare Iterator).
_NOT_SSE: Any = object()


def _sse_payload_type(annotation: Any) -> Any:
    """If ``annotation`` is one of the SSE-shaped iterables / generators
    (sync or async) or :class:`EventStream`, return the element type
    (or :data:`_NOT_SSE`)."""
    origin = get_origin(annotation)
    if origin is None:
        return _NOT_SSE
    # ``EventStream[T]`` — origin is ``EventStream`` itself.
    if origin is EventStream:
        args = get_args(annotation)
        return args[0] if args else None
    # ``Generator[T, send, return]`` / ``Iterator[T]`` / ``Iterable[T]``
    # plus their async counterparts. All live in ``collections.abc``.
    if origin not in (Generator, Iterator, Iterable, AsyncGenerator, AsyncIterator, AsyncIterable):
        return _NOT_SSE
    args = get_args(annotation)
    return args[0] if args else None


def build_responses(shapes: tuple[ResponseShape, ...], registry: SchemaRegistry) -> dict[str, openapi_spec.Response]:
    responses: dict[str, openapi_spec.Response] = {}
    for shape in shapes:
        if shape.body_type is None:
            responses[str(shape.status_code)] = openapi_spec.Response(description=shape.description)
            continue
        schema = registry.schema_for(shape.body_type)
        responses[str(shape.status_code)] = openapi_spec.Response(
            description=shape.description,
            content={shape.content_type: openapi_spec.MediaType(schema=schema)},
        )
    return responses


# --- Response building ---------------------------------------------------

_TEXT_CONTENT_TYPE = b"text/plain; charset=utf-8"
_OCTET_CONTENT_TYPE = b"application/octet-stream"


def encode_body(value: object, adapters: AdapterRegistry) -> tuple[bytes, bytes]:
    """Return ``(body_bytes, content_type)`` for ``value``.

    Pass-through for ``bytes`` / ``str``; structured values go through the
    :class:`TypeAdapter` that claims ``type(value)``.
    """
    if value is None:
        return b"", b""
    if isinstance(value, bytes):
        return value, _OCTET_CONTENT_TYPE
    if isinstance(value, bytearray):
        return bytes(value), _OCTET_CONTENT_TYPE
    if isinstance(value, str):
        return value.encode("utf-8"), _TEXT_CONTENT_TYPE
    body, content_type = adapters.for_value(value).encode(value)
    return body, content_type.encode("ascii")


def build_http_response(result: OpResult, adapters: AdapterRegistry) -> tuple[_Response, bytes]:
    body_bytes, default_ct = encode_body(result.body, adapters)
    headers: list[tuple[bytes, bytes]] = []
    headers_seen: set[bytes] = set()
    for name, value in iter_response_headers(result.headers):
        headers.append((name, value))
        headers_seen.add(name.lower())
    if body_bytes and b"content-type" not in headers_seen and default_ct:
        headers.append((b"content-type", default_ct))
    if b"content-length" not in headers_seen:
        headers.append((b"content-length", str(len(body_bytes)).encode("ascii")))
    return _Response(status_code=result.status_code, headers=headers), body_bytes


def iter_response_headers(headers: Mapping[str, str]):
    for name, value in headers.items():
        yield name.encode("ascii"), value.encode("iso-8859-1")


# --- SSE detection -------------------------------------------------------

SSE_RESPONSE_HEADERS: list[tuple[bytes, bytes]] = [
    (b"content-type", b"text/event-stream; charset=utf-8"),
    (b"cache-control", b"no-cache"),
    (b"x-accel-buffering", b"no"),  # Disable proxy buffering (nginx).
]


def is_sse_payload(value: object) -> bool:
    """True if ``value`` should be streamed as SSE (sync side).

    Recognises explicit :class:`EventStream` and any ``Iterator`` (the
    result of calling a generator function or building one explicitly).
    Bare ``Iterable`` is too broad — ``list`` / ``tuple`` / ``dict`` are
    all iterable but should land in JSON.
    """
    return isinstance(value, (EventStream, Iterator))


def is_async_sse_payload(value: object) -> bool:
    """True if ``value`` should be streamed as SSE (async side).

    Recognises explicit :class:`EventStream` and any ``AsyncIterator``
    (the result of calling an ``async def`` generator function). Plain
    sync iterators are *not* recognised here — the async runtime rejects
    them with a clear error so users don't accidentally block the event
    loop with a sync generator.
    """
    return isinstance(value, (EventStream, AsyncIterator))


def is_sync_iterable(value: object) -> bool:
    """True if ``value`` is a sync iterator/generator (and not an
    :class:`EventStream`). Used by the async runtime to detect — and
    reject — sync generators in async handlers."""
    if isinstance(value, EventStream):
        return False
    return isinstance(value, Iterator)
