"""``HttpApp`` — type-driven OpenAPI 3.2 framework on top of localpost.http.

The user surface mirrors FastAPI's decorator API. Each registered
operation becomes a :class:`localpost.openapi.operation.Operation` whose
arg resolvers, response shapes, and OpenAPI doc contributions are derived
from the function signature.

::

    from localpost import hosting
    from localpost.http import ServerConfig
    from localpost.openapi import HttpApp, NotFound

    app = HttpApp()


    @app.get("/hello/{name}")
    def hello(name: str) -> str:
        return f"Hello, {name}!"


    sys.exit(hosting.run_app(app.service(ServerConfig(port=8000))))
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import replace
from http import HTTPMethod
from typing import Any, Literal

from localpost import hosting
from localpost.http._pool import thread_pool_handler
from localpost.http._service import http_server
from localpost.http._types import Response
from localpost.http.config import ServerConfig
from localpost.http.router import Routes
from localpost.http.server import HTTPReqCtx, RequestHandler
from localpost.openapi import spec as openapi_spec
from localpost.openapi._docs import redoc_html, scalar_html, swagger_html
from localpost.openapi.filter import OpFilter
from localpost.openapi.operation import Operation
from localpost.openapi.schemas import SchemaRegistry

__all__ = ["HttpApp"]


_FluentDecorator = Callable[[Callable[..., Any]], Callable[..., Any]]
DocsUI = Literal["swagger", "redoc", "scalar", "all"]


class HttpApp:
    """Type-driven HTTP application that emits an OpenAPI 3.2 spec.

    Args:
        info: Top-level OpenAPI :class:`Info` block.
        filters: App-level :class:`OpFilter` s. Run before the per-operation
            resolvers on every request, in order. Their
            :meth:`OpFilter.contribute_root` is called once at spec build;
            their :meth:`OpFilter.contribute_operation` is called for every
            operation.
        max_concurrency: Worker-pool size used by :func:`thread_pool_handler`.
            Default 32.
        backlog: Extra channel buffer between the selector and the worker
            pool. Default ``0`` (rendezvous).
        openapi_path: URL the generated spec is served on. ``None`` to
            disable.
        docs_path: Base URL for the built-in doc UIs. Each UI is served
            under it: ``{docs_path}`` (Swagger), ``{docs_path}/redoc``,
            ``{docs_path}/scalar``. ``None`` to disable all UIs.
        docs_ui: Which doc UIs to mount. Default ``"all"``.
    """

    def __init__(
        self,
        *,
        info: openapi_spec.Info | None = None,
        filters: Sequence[OpFilter] = (),
        max_concurrency: int = 32,
        backlog: int = 0,
        openapi_path: str | None = "/openapi.json",
        docs_path: str | None = "/docs",
        docs_ui: DocsUI = "all",
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        if backlog < 0:
            raise ValueError("backlog must be >= 0")
        self._info = info or openapi_spec.Info()
        self._filters = tuple(filters)
        self._max_concurrency = max_concurrency
        self._backlog = backlog
        self._openapi_path = openapi_path
        self._docs_path = docs_path
        self._docs_ui = docs_ui
        self._operations: list[Operation] = []
        self._lock = threading.Lock()
        # Cached spec; invalidated whenever an operation is added.
        self._cached_spec: openapi_spec.OpenAPI | None = None
        self._cached_spec_bytes: bytes | None = None

    # ----- Decorators -----

    def get(self, path: str, *, filters: Sequence[OpFilter] = ()) -> _FluentDecorator:
        return self._decorator(HTTPMethod.GET, path, filters)

    def post(self, path: str, *, filters: Sequence[OpFilter] = ()) -> _FluentDecorator:
        return self._decorator(HTTPMethod.POST, path, filters)

    def put(self, path: str, *, filters: Sequence[OpFilter] = ()) -> _FluentDecorator:
        return self._decorator(HTTPMethod.PUT, path, filters)

    def delete(self, path: str, *, filters: Sequence[OpFilter] = ()) -> _FluentDecorator:
        return self._decorator(HTTPMethod.DELETE, path, filters)

    def patch(self, path: str, *, filters: Sequence[OpFilter] = ()) -> _FluentDecorator:
        return self._decorator(HTTPMethod.PATCH, path, filters)

    def _decorator(self, method: HTTPMethod, path: str, op_filters: Sequence[OpFilter]) -> _FluentDecorator:
        # App-level filters run first; per-op filters extend them.
        combined = (*self._filters, *op_filters)

        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            op = Operation.create(method, path, fn, filters=combined)
            with self._lock:
                self._operations.append(op)
                self._cached_spec = None
                self._cached_spec_bytes = None
            return fn

        return deco

    # ----- Spec -----

    @property
    def operations(self) -> Sequence[Operation]:
        return tuple(self._operations)

    @property
    def openapi_doc(self) -> openapi_spec.OpenAPI:
        """Return the (cached) OpenAPI 3.2 document for the registered ops."""
        with self._lock:
            cached = self._cached_spec
            if cached is not None:
                return cached
            registry = SchemaRegistry()
            doc = openapi_spec.OpenAPI(info=self._info)
            # Every filter (app-level and per-op) gets its contribute_root
            # called exactly once. We dedupe by identity so the same filter
            # attached to several ops registers its securityScheme just once.
            seen: set[int] = set()
            for f in self._all_filters():
                key = id(f)
                if key in seen:
                    continue
                seen.add(key)
                doc = f.contribute_root(doc, registry)
            for op in self._operations:
                spec_op = op.build_spec(registry)
                doc = doc.add_operation(op.path, op.method.value, spec_op)
            # Merge in the schemas the registry collected. Filters may have
            # already populated other Components fields above; preserve them
            # by replacing only ``schemas``.
            doc = doc.with_components(
                replace(doc.components, schemas={**doc.components.schemas, **registry.components()})
            )
            self._cached_spec = doc
            return doc

    def _all_filters(self):
        """Yield every filter that participates in the doc — app-level
        first, then per-op (in registration order)."""
        yield from self._filters
        for op in self._operations:
            yield from op.filters

    def _openapi_bytes(self) -> bytes:
        with self._lock:
            cached = self._cached_spec_bytes
            if cached is not None:
                return cached
        # Compute outside the lock so doc rendering doesn't hold contention.
        body = self.openapi_doc.to_json()
        with self._lock:
            self._cached_spec_bytes = body
        return body

    # ----- Hosting -----

    def service(
        self,
        config: ServerConfig,
        *,
        selectors: int = 1,
        acceptor: bool = False,
    ) -> hosting.ServiceF:
        """Return a :func:`localpost.hosting.service` running this app.

        Composes a worker pool (via :func:`thread_pool_handler`) and the
        HTTP server (via :func:`http_server`). The user fn for each
        registered operation runs on a worker after the request body is
        buffered by the selector.

        ``selectors`` and ``acceptor`` forward to :func:`http_server`.
        """
        max_concurrency = self._max_concurrency
        backlog = self._backlog
        router = self._build_router_handler()

        @hosting.service
        async def _app_service():
            async with thread_pool_handler(router, max_concurrency=max_concurrency, backlog=backlog) as h:
                async with http_server(config, h, selectors=selectors, acceptor=acceptor):
                    yield

        return _app_service()

    # ----- Internals -----

    def _build_router_handler(self) -> RequestHandler:
        routes = Routes()
        for op in self._operations:
            routes.add(op.method, op.path, op.as_handler())
        self._mount_built_in(routes)
        return routes.build().as_handler()

    def _mount_built_in(self, routes: Routes) -> None:
        openapi_path = self._openapi_path
        if openapi_path:
            self_ref = self

            def openapi_handler(ctx: HTTPReqCtx):
                body = self_ref._openapi_bytes()
                response = Response(
                    status_code=200,
                    headers=[
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                    ],
                )
                ctx.complete(response, body)

            routes.add(HTTPMethod.GET, openapi_path, openapi_handler)
        docs_path = self._docs_path
        if docs_path and self._openapi_path:
            ui = self._docs_ui
            if ui in ("swagger", "all"):
                self._add_html_route(routes, docs_path, swagger_html(self._openapi_path))
            if ui in ("redoc", "all"):
                self._add_html_route(routes, f"{docs_path}/redoc", redoc_html(self._openapi_path))
            if ui in ("scalar", "all"):
                self._add_html_route(routes, f"{docs_path}/scalar", scalar_html(self._openapi_path))

    @staticmethod
    def _add_html_route(routes: Routes, path: str, body: bytes) -> None:
        response = Response(
            status_code=200,
            headers=[
                (b"content-type", b"text/html; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        )

        def handler(ctx: HTTPReqCtx):
            ctx.complete(response, body)

        routes.add(HTTPMethod.GET, path, handler)
