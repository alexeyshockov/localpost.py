from __future__ import annotations

import inspect
import json
import logging
import threading
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from http import HTTPMethod
from typing import Annotated, Protocol, Self, final

import localpost.spec.openapi as openapi_spec
from localpost.http.router import RequestHandler, Router

FluentOpDecorator = Callable[[Callable[..., object]], RequestHandler]


@final
class HttpApp:
    def __init__(self):
        self._router: Router | None = None
        self._router_lock: threading.Lock = threading.Lock()
        self._path_ops: list[FluentPathOp] = []

    def __call__(self, request: HTTPContext) -> None:
        self.router(request)

    def wsgi(self, environ, start_response):
        """WSGI app, to be used with any WSGI server, e.g. Granian."""
        return self.router.wsgi(environ, start_response)

    def register_arg_resolver(self, resolver_factory: ArgResolverFactory) -> None:
        # TODO Register a custom ArgResolverFactory for handling custom types in path operation parameters
        pass

    def register_result_resolver(self, resolver_factory: ArgResolverFactory) -> None:
        # TODO Register a custom ArgResolverFactory for handling custom types in path operation parameters
        pass

    @property
    def docs(self) -> openapi_spec.OpenAPI:
        pass

    @property
    def router(self) -> Router:
        if router := self._router:
            return router
        with self._router_lock:
            if not self._router:
                self._router = self._create_router()
            return self._router

    def _create_router(self) -> Router:
        # FIXME Go through all registered FluentPathOps, convert their path patterns to regexes, group by path regex, create a Router with the resulting list of (regex, {method: handler}) pairs
        pass

    def register(self, op: FluentPathOp) -> FluentPathOp:
        self._path_ops.append(op)
        with self._router_lock:
            self._router = None
        return op

    def get(self, path: str) -> FluentOpDecorator:
        """Decorator to register a GET operation on the given path."""

        def decorator(func) -> RequestHandler:
            return self.register(FluentPathOp(HTTPMethod.GET, path, func))

        return decorator

    def post(self, path: str) -> FluentOpDecorator:
        """Decorator to register a POST operation on the given path."""

        def decorator(func) -> RequestHandler:
            return self.register(FluentPathOp(HTTPMethod.POST, path, func))

        return decorator


@final
@dataclass(eq=False, frozen=True)
class FluentPathOp:
    method: HTTPMethod
    path: str
    """Path pattern, like /books/{id} or /shop/{name}/checkout."""

    target: Callable[..., object]
    arg_resolvers: Mapping[str, ArgResolver]
    response_resolver: ResponseResolver
    """
    To resolve (create) a response from the target's return value. 

    E.g. if the target returns a dict, we can serialize it to JSON and set the Content-Type header.
    """

    # # Security is for sure out of our scope, so this should be declared manually by the user
    # security: openapi_spec.SecurityScheme | None = None
    # TODO Simply supply a doc customizer...

    @classmethod
    def create(cls, method: HTTPMethod, path: str, func, /) -> Self:
        pass  # FIXME Implement: inspect func signature, create ArgResolvers for its parameters, create a FluentPathOpHandler with the target and resolvers

    # Build it all in the root app...
    # @cached_property
    # def docs(self) -> openapi_spec.OpenAPIRoute:
    #     pass

    def __call__(self, request: RESTContext) -> None:
        args = {name: resolver(request) for name, resolver in self.arg_resolvers.items()}
        return_value = self.target(**args)
        self.response_resolver(request, return_value)


# class ResponseResolverFactory(Protocol):
#     def __call__(self, return_annotation: type, /) -> ResponseResolver:
#         ...


class ResponseResolver(Protocol):
    def __call__(self, result: object, ctx: RESTContext, http_ctx: HTTPContext, /) -> None:
        # if generator — run the generator and send chunks as they are produced, set Content-Type to text/event-stream
        # if File — set Content-Type according to the file type, send file in chunks
        # ...
        ...


class DefaultResponseResolverFactory:
    def __call__(self, return_annotation: type, /) -> ResponseResolver:
        # TODO Implement a default resolver that handles common types (str, dict, etc.) and serializes them to the appropriate format according to the Accept header (e.g. JSON for dict)
        pass


# class Redirect:  # Not an exception, but a valid return type for a path operation, to be handled by a custom ResponseResolver
#     pass
#
#
# class FileResponse:
#     pass
#
#
# class SSEResponse:
#     pass


class ArgResolverFactory:
    def __call__(self, param: inspect.Parameter, /) -> ArgResolver: ...


class ArgResolver(Protocol):
    def __call__(self, request: RESTContext, /) -> object:
        """
        Resolve an argument for the target function from the request context.

        Raises ValueError if case of any issues, e.g. missing required header or query param, invalid path param
        format, etc.
        """
        ...


@final
@dataclass(kw_only=True, frozen=True, slots=True)
class FromBody(ArgResolverFactory):
    """
    Built-in body resolver that supports most common Python types (dataclasses, Pydantic, msgpack) and input
    formats (JSON, msgpack).

    For any complex case, just implement a custom ArgResolver and attach it to the function parameter via Annotated.
    """

    json_converter: Callable[[bytes], object] | None = None

    def __call__(self, param: inspect.Parameter, /) -> ArgResolver:
        json_converter = self.json_converter or json.loads

        if param.annotation is inspect.Parameter.empty:
            assert param.default is inspect.Parameter.empty  # Body is required (more for simplicity for now)

            def resolve_any(request: RESTContext) -> object:
                if not (content_type := request.get_header("content-type")):
                    raise ValueError("Missing Content-Type header")
                if "json" in content_type:
                    return json_converter(request.body.read())
                raise ValueError(f"Unsupported Content-Type: {content_type}")

            return resolve_any

        param_type = param.annotation  # TODO Deal with Annotated, Optional, etc.
        assert isinstance(param_type, type)

        # TODO Decode Pydantic models with Pydantic, msgspec.Struct with msgspec
        # TODO Decode dataclasses with msgspec
        def resolve(request: RESTContext) -> object:
            pass

        return resolve


@final
@dataclass(frozen=True, slots=True)
class FromHeader(ArgResolverFactory):
    """Resolve from a header, e.g. X-User-Id."""

    name: str | None = None
    converter: Callable[[str], object] | None = None

    def __call__(self, param: inspect.Parameter, /) -> ArgResolver:
        pass  # TODO Implement, similar to FromQuery and FromPath


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
            param_type = param.annotation  # TODO Deal with Annotated, Optional, etc.
            assert isinstance(param_type, type)
            if issubclass(param_type, Collection):  # list, set, etc.
                converter = param_type
            else:
                converter = lambda values: param_type(values[0])
        else:  # First argument as a string by default, if not annotated and no converter provided
            converter = lambda values: values[0]

        def resolve(request: RESTContext) -> object:
            if param.default is inspect.Parameter.empty:
                return converter(request.query_args[param_name])
            return converter(request.query_args.get(param_name, param.default))

        return resolve


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
            param_type = param.annotation  # TODO Deal with Annotated, Optional, etc.
            converter = param_type
        else:
            converter = str

        def resolve(request: RESTContext) -> object:
            if param.default is inspect.Parameter.empty:
                return converter(request.path_args[param_name])
            return converter(request.path_args.get(param_name, param.default))

        return resolve


class Example:
    """Example value for a parameter or return type, to be included in the OpenAPI docs."""

    def __init__(self, value: object):
        self.value = value
