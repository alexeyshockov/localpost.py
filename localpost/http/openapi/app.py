from __future__ import annotations

import inspect
import json
import threading
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from http import HTTPMethod
from typing import Annotated, ParamSpec, Protocol, Self, TypeVar, final, get_args, get_origin

import msgspec

import localpost.spec.openapi as openapi_spec
from localpost.http.router import RequestCtx, RequestHandler, Response, Router
from localpost.http.uritemplate import URITemplate

P = ParamSpec("P")
R = TypeVar("R")
FluentOpDecorator = Callable[[Callable[P, R]], Callable[P, R]]


# --- OpResult hierarchy ---


@dataclass(frozen=True, slots=True)
class OpResult:
    code: int
    headers: Mapping[str, str]
    body: object

    def __init__(self, code: int, body: object, /, *, headers: Mapping[str, str] | None = None):
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "headers", headers or {})


class Ok[T](OpResult):
    def __init__(self, body: T, /, *, headers: dict[str, str] | None = None):
        OpResult.__init__(self, 200, body, headers=headers)


class Created[T](OpResult):
    def __init__(self, body: T, /, *, headers: dict[str, str] | None = None):
        OpResult.__init__(self, 201, body, headers=headers)


class NoContent(OpResult):
    def __init__(self, /, *, headers: dict[str, str] | None = None):
        OpResult.__init__(self, 204, None, headers=headers)


class BadRequest[T](OpResult):
    def __init__(self, body: T, /, *, headers: dict[str, str] | None = None):
        OpResult.__init__(self, 400, body, headers=headers)


class NotFound[T](OpResult):
    def __init__(self, body: T, /, *, headers: dict[str, str] | None = None):
        OpResult.__init__(self, 404, body, headers=headers)


# --- Protocols ---


class ArgResolver(Protocol):
    def __call__(self, request: RequestCtx, /) -> object: ...


class ArgResolverFactory:
    def __call__(self, param: inspect.Parameter, /) -> ArgResolver: ...


class ResponseResolver(Protocol):
    def __call__(self, result: OpResult, /) -> Response: ...


# --- Resolver implementations ---


@final
class DefaultResponseResolver:
    def __call__(self, result: OpResult, /) -> Response:
        headers: dict[str, str] = dict(result.headers)

        if result.body is None:
            return Response(status_code=result.code, headers=headers, body=[])

        if isinstance(result.body, bytes):
            body_bytes = result.body
            headers.setdefault("Content-Type", "application/octet-stream")
        elif isinstance(result.body, str):
            body_bytes = msgspec.json.encode(result.body)
            headers.setdefault("Content-Type", "application/json")
        else:
            try:
                body_bytes = msgspec.json.encode(result.body)
            except Exception:
                body_bytes = json.dumps(result.body).encode()
            headers.setdefault("Content-Type", "application/json")

        headers["Content-Length"] = str(len(body_bytes))
        return Response(status_code=result.code, headers=headers, body=[body_bytes])


@final
@dataclass(kw_only=True, frozen=True, slots=True)
class FromBody(ArgResolverFactory):
    """Built-in body resolver."""

    json_converter: Callable[[bytes], object] | None = None

    def __call__(self, param: inspect.Parameter, /) -> ArgResolver:
        json_converter = self.json_converter or json.loads

        def resolve(request: RequestCtx) -> object:
            return json_converter(request.body())

        return resolve


@final
@dataclass(frozen=True, slots=True)
class FromHeader(ArgResolverFactory):
    """Resolve from a header, e.g. X-User-Id."""

    name: str | None = None
    converter: Callable[[str], object] | None = None

    def __call__(self, param: inspect.Parameter, /) -> ArgResolver:
        header_name = self.name or param.name.replace("_", "-").lower()
        converter = self.converter or str
        has_default = param.default is not inspect.Parameter.empty

        def resolve(request: RequestCtx) -> object:
            value = request.headers.get(header_name)
            if value is None:
                if has_default:
                    return param.default
                raise ValueError(f"Missing required header: {header_name}")
            return converter(value)

        return resolve


@final
@dataclass(frozen=True, slots=True)
class FromQuery(ArgResolverFactory):
    """Resolve from a query parameter, e.g. /books?author=tolkien."""

    param_name: str | None = None
    converter: Callable[[Sequence[str]], object] | None = None

    def __call__(self, param: inspect.Parameter, /) -> ArgResolver:
        param_name = self.param_name or param.name
        has_default = param.default is not inspect.Parameter.empty
        if self.converter:
            converter = self.converter
        elif param.annotation is not inspect.Parameter.empty:
            param_type = param.annotation
            if get_origin(param_type) is Annotated:
                param_type = get_args(param_type)[0]
            if isinstance(param_type, type) and issubclass(param_type, Collection) and param_type is not str:
                converter = param_type
            elif isinstance(param_type, type):
                converter = lambda values, _t=param_type: _t(values[0])
            else:
                converter = lambda values: values[0]
        else:
            converter = lambda values: values[0]

        def resolve(request: RequestCtx) -> object:
            values = request.query_args.get(param_name)
            if values is None:
                if has_default:
                    return param.default
                raise ValueError(f"Missing required query parameter: {param_name}")
            return converter(values)

        return resolve


@final
@dataclass(frozen=True, slots=True)
class FromPath(ArgResolverFactory):
    """Resolve from a path parameter, e.g. /books/{id}."""

    param_name: str | None = None
    converter: Callable[[str], object] | None = None

    def __call__(self, param: inspect.Parameter, /) -> ArgResolver:
        param_name = self.param_name or param.name
        if self.converter:
            converter = self.converter
        elif param.annotation is not inspect.Parameter.empty:
            param_type = param.annotation
            if get_origin(param_type) is Annotated:
                param_type = get_args(param_type)[0]
            if isinstance(param_type, type):
                converter = param_type
            else:
                converter = str
        else:
            converter = str

        def resolve(request: RequestCtx) -> object:
            value = request.path_args.get(param_name)
            if value is None:
                raise ValueError(f"Missing path parameter: {param_name}")
            return converter(value)

        return resolve


def _extract_resolver_factory(annotation) -> ArgResolverFactory | None:
    """Extract an ArgResolverFactory from an Annotated type, if present."""
    if get_origin(annotation) is Annotated:
        for arg in get_args(annotation):
            if isinstance(arg, ArgResolverFactory):
                return arg
    return None


# --- FluentPathOp ---


@final
@dataclass(eq=False, frozen=True)
class FluentPathOp:
    method: HTTPMethod
    path: str
    """Path pattern, like /books/{id} or /shop/{name}/checkout."""

    target: Callable[..., object]
    arg_resolvers: Mapping[str, ArgResolver]
    response_resolver: ResponseResolver

    @classmethod
    def create(cls, method: HTTPMethod, path: str, func: Callable, /) -> Self:
        template = URITemplate.parse(path)
        path_var_names = set(template.variable_names)

        sig = inspect.signature(func)
        arg_resolvers: dict[str, ArgResolver] = {}

        for param_name, param in sig.parameters.items():
            annotation = param.annotation
            resolver_factory = _extract_resolver_factory(annotation)

            if resolver_factory is not None:
                arg_resolvers[param_name] = resolver_factory(param)
            elif param_name in path_var_names:
                arg_resolvers[param_name] = FromPath()(param)
            else:
                arg_resolvers[param_name] = FromQuery()(param)

        return cls(
            method=method,
            path=path,
            target=func,
            arg_resolvers=arg_resolvers,
            response_resolver=DefaultResponseResolver(),
        )

    def as_handler(self) -> RequestHandler:
        def handler(ctx: RequestCtx) -> Response:
            return self(ctx)

        return handler

    def __call__(self, request: RequestCtx) -> Response:
        args = {}
        for name, resolver in self.arg_resolvers.items():
            result = resolver(request)
            if isinstance(result, OpResult):
                return self.response_resolver(result)
            args[name] = result
        return_value = self.target(**args)
        if isinstance(return_value, OpResult):
            op_result = return_value
        else:
            op_result = Ok(return_value)
        return self.response_resolver(op_result)


# --- HttpApp ---


@final
class HttpApp:
    def __init__(self, *, info: openapi_spec.Info | None = None):
        self._router: Router | None = None
        self._router_lock: threading.Lock = threading.Lock()
        self._path_ops: list[FluentPathOp] = []
        self._info = info or openapi_spec.Info()

    def wsgi(self, environ, start_response):
        """WSGI app, to be used with any WSGI server, e.g. Granian."""
        return self.router.wsgi(environ, start_response)

    @property
    def docs(self) -> openapi_spec.OpenAPI:
        spec = openapi_spec.OpenAPI(info=self._info)
        for op in self._path_ops:
            params: list[openapi_spec.Parameter] = []
            template = URITemplate.parse(op.path)
            for var_name in template.variable_names:
                params.append(
                    openapi_spec.Parameter(name=var_name, location="path", required=True, schema={"type": "string"})
                )
            for arg_name in op.arg_resolvers:
                if arg_name not in template.variable_names:
                    params.append(
                        openapi_spec.Parameter(
                            name=arg_name, location="query", required=True, schema={"type": "string"}
                        )
                    )
            operation = openapi_spec.Operation(
                summary=op.target.__doc__ or f"{op.method.value} {op.path}",
                operation_id=f"{op.method.value.lower()}_{op.path.replace('/', '_').strip('_')}",
                parameters=tuple(params),
                responses={
                    "200": openapi_spec.Response(
                        description="Successful response",
                        content={"application/json": openapi_spec.MediaType(schema={"type": "string"})},
                    )
                },
            )
            spec = spec.add_operation(op.path, op.method.value, operation)
        return spec

    @property
    def router(self) -> Router:
        if router := self._router:
            return router
        with self._router_lock:
            if not self._router:
                self._router = self._create_router()
            return self._router

    def _create_router(self) -> Router:
        paths: dict[URITemplate, dict[HTTPMethod, RequestHandler]] = {}

        for op in self._path_ops:
            template = URITemplate.parse(op.path)
            # Find existing template with same string
            target_key: URITemplate | None = None
            for existing in paths:
                if existing.template == template.template:
                    target_key = existing
                    break
            if target_key is None:
                paths[template] = {op.method: op.as_handler()}
            else:
                paths[target_key][op.method] = op.as_handler()

        # Built-in /openapi.json route
        openapi_template = URITemplate.parse("/openapi.json")
        app_ref = self

        def openapi_handler(ctx: RequestCtx) -> Response:
            return Response(
                status_code=200,
                headers={"Content-Type": "application/json"},
                body=[app_ref.docs.to_json()],
            )

        paths[openapi_template] = {HTTPMethod.GET: openapi_handler}
        return Router(paths=paths)

    def register(self, op: FluentPathOp) -> FluentPathOp:
        self._path_ops.append(op)
        with self._router_lock:
            self._router = None
        return op

    def get(self, path: str) -> FluentOpDecorator:
        """Decorator to register a GET operation on the given path."""

        def decorator(func):
            self.register(FluentPathOp.create(HTTPMethod.GET, path, func))
            return func

        return decorator

    def post(self, path: str) -> FluentOpDecorator:
        """Decorator to register a POST operation on the given path."""

        def decorator(func):
            self.register(FluentPathOp.create(HTTPMethod.POST, path, func))
            return func

        return decorator

    def put(self, path: str) -> FluentOpDecorator:
        """Decorator to register a PUT operation on the given path."""

        def decorator(func):
            self.register(FluentPathOp.create(HTTPMethod.PUT, path, func))
            return func

        return decorator

    def delete(self, path: str) -> FluentOpDecorator:
        """Decorator to register a DELETE operation on the given path."""

        def decorator(func):
            self.register(FluentPathOp.create(HTTPMethod.DELETE, path, func))
            return func

        return decorator
