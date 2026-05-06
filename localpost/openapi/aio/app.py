"""``HttpAsyncApp`` — async sibling of :class:`localpost.openapi.HttpApp`.

Same decorator API, same OpenAPI doc emission, same ``OpResult``
hierarchy. The user fns are ``async def``; the app deploys to ASGI by
default — :meth:`asgi` returns an ASGI 3 callable suitable for
``uvicorn``, ``hypercorn``, or ``granian --interface asgi``.
:meth:`service` wires up uvicorn under :mod:`localpost.hosting` for
``hosting.run_app(app.service(config))`` parity with the sync flavour.
"""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from http import HTTPMethod
from typing import Any, Literal

import anyio

from localpost.http._types import Response
from localpost.http.router import RouteMatch, URITemplate
from localpost.openapi import spec as openapi_spec
from localpost.openapi._docs import redoc_html, scalar_html, swagger_html
from localpost.openapi.adapters import AdapterRegistry, default_registry
from localpost.openapi.aio._ctx import (
    ASGIReceive,
    ASGIScope,
    ASGISend,
    _ASGIReqCtx,
    addrs_from_scope,
    build_request_from_scope,
    read_body,
)
from localpost.openapi.aio.middleware import AsyncOpMiddleware
from localpost.openapi.aio.operation import AsyncOperation
from localpost.openapi.schemas import SchemaRegistry

__all__ = ["HttpAsyncApp"]


_FluentDecorator = Callable[[Callable[..., Any]], Callable[..., Any]]
DocsUI = Literal["swagger", "redoc", "scalar", "all"]

# Per-request callable: builds and writes the response over ``ctx``.
type _AsyncHandler = Callable[[_ASGIReqCtx], Awaitable[None]]


class HttpAsyncApp:
    """Async type-driven HTTP application that emits an OpenAPI 3.2 spec.

    Mirrors :class:`localpost.openapi.HttpApp` argument-for-argument; the
    differences are:

    - Handlers must be ``async def`` (or ``async def`` generators for SSE).
    - Middlewares must implement :class:`AsyncOpMiddleware`.
    - :meth:`asgi` returns an ASGI 3 callable; :meth:`service` runs it
      under uvicorn (or hypercorn) via :mod:`localpost.hosting`.

    Args:
        info: Top-level OpenAPI :class:`Info` block.
        middlewares: App-level :class:`AsyncOpMiddleware`-s.
        openapi_path: URL the generated spec is served on; ``None`` to disable.
        docs_path: Base URL for the built-in doc UIs.
        docs_ui: Which doc UIs to mount.
        adapters: Type adapters used for JSON Schema / decode / encode.
        max_body_size: Maximum request-body bytes buffered before
            dispatch. ``-1`` disables the limit. Defaults to 1 MiB.
    """

    def __init__(
        self,
        *,
        info: openapi_spec.Info | None = None,
        middlewares: Sequence[AsyncOpMiddleware] = (),
        openapi_path: str | None = "/openapi.json",
        docs_path: str | None = "/docs",
        docs_ui: DocsUI = "all",
        adapters: AdapterRegistry | None = None,
        max_body_size: int = 1 << 20,
    ) -> None:
        for mw in middlewares:
            _ensure_async_middleware(mw)
        self._info = info or openapi_spec.Info()
        self._middlewares = tuple(middlewares)
        self._openapi_path = openapi_path
        self._docs_path = docs_path
        self._docs_ui = docs_ui
        self._adapters = adapters or default_registry()
        self._max_body_size = max_body_size
        self._operations: list[AsyncOperation] = []
        self._lock = threading.Lock()
        self._cached_spec: openapi_spec.OpenAPI | None = None
        self._cached_spec_bytes: bytes | None = None

    # ----- Decorators -----

    def get(self, path: str, *, middlewares: Sequence[AsyncOpMiddleware] = ()) -> _FluentDecorator:
        return self._decorator(HTTPMethod.GET, path, middlewares)

    def post(self, path: str, *, middlewares: Sequence[AsyncOpMiddleware] = ()) -> _FluentDecorator:
        return self._decorator(HTTPMethod.POST, path, middlewares)

    def put(self, path: str, *, middlewares: Sequence[AsyncOpMiddleware] = ()) -> _FluentDecorator:
        return self._decorator(HTTPMethod.PUT, path, middlewares)

    def delete(self, path: str, *, middlewares: Sequence[AsyncOpMiddleware] = ()) -> _FluentDecorator:
        return self._decorator(HTTPMethod.DELETE, path, middlewares)

    def patch(self, path: str, *, middlewares: Sequence[AsyncOpMiddleware] = ()) -> _FluentDecorator:
        return self._decorator(HTTPMethod.PATCH, path, middlewares)

    def _decorator(
        self,
        method: HTTPMethod,
        path: str,
        op_middlewares: Sequence[AsyncOpMiddleware],
    ) -> _FluentDecorator:
        for mw in op_middlewares:
            _ensure_async_middleware(mw)
        combined = (*self._middlewares, *op_middlewares)

        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            op = AsyncOperation.create(method, path, fn, middlewares=combined, adapters=self._adapters)
            with self._lock:
                self._operations.append(op)
                self._cached_spec = None
                self._cached_spec_bytes = None
            return fn

        return deco

    # ----- Spec -----

    @property
    def operations(self) -> Sequence[AsyncOperation]:
        return tuple(self._operations)

    @property
    def openapi_doc(self) -> openapi_spec.OpenAPI:
        """Return the (cached) OpenAPI 3.2 document."""
        with self._lock:
            cached = self._cached_spec
            if cached is not None:
                return cached
            registry = SchemaRegistry(self._adapters)
            doc = openapi_spec.OpenAPI(info=self._info)
            seen: set[int] = set()
            for mw in self._all_middlewares():
                key = id(mw)
                if key in seen:
                    continue
                seen.add(key)
                doc = mw.contribute_root(doc, registry)
            for op in self._operations:
                spec_op = op.build_spec(registry)
                doc = doc.add_operation(op.path, op.method.value, spec_op)
            doc = doc.with_components(
                replace(doc.components, schemas={**doc.components.schemas, **registry.components()})
            )
            self._cached_spec = doc
            return doc

    def _all_middlewares(self):
        yield from self._middlewares
        for op in self._operations:
            yield from op.middlewares

    def _openapi_bytes(self) -> bytes:
        with self._lock:
            cached = self._cached_spec_bytes
            if cached is not None:
                return cached
        body = self.openapi_doc.to_json()
        with self._lock:
            self._cached_spec_bytes = body
        return body

    # ----- Hosting -----

    def asgi(self) -> Callable[[ASGIScope, ASGIReceive, ASGISend], Awaitable[None]]:
        """Return an ASGI 3 callable that dispatches this app.

        Deploy with e.g. ``uvicorn myapp:asgi_app`` or
        ``granian --interface asgi myapp:asgi_app``::

            app = HttpAsyncApp()
            asgi_app = app.asgi()

        The returned callable handles ``lifespan`` (no-op) and ``http``
        scopes. WebSocket scopes are rejected.
        """
        return _AsgiDispatch.from_app(self)

    def service(
        self,
        config: Any,
        *,
        server: Literal["uvicorn", "hypercorn"] = "uvicorn",
    ):
        """Return a :func:`localpost.hosting.service` running this app.

        ``config`` is the host server's config object —
        :class:`uvicorn.Config` for ``server="uvicorn"`` (the default) or
        :class:`hypercorn.Config` for ``server="hypercorn"``. Both
        servers are configured with the ASGI app from :meth:`asgi`.

        Pulls the ``localpost.hosting.services.uvicorn`` /
        ``hypercorn`` adapters lazily so the import only happens when
        the user picks that server.
        """
        asgi_app = self.asgi()
        if server == "uvicorn":
            from localpost.hosting.services.uvicorn import uvicorn_server  # noqa: PLC0415

            config.app = asgi_app
            return uvicorn_server(config)
        if server == "hypercorn":
            from localpost.hosting.services.hypercorn import hypercorn_server  # noqa: PLC0415

            return hypercorn_server(asgi_app, config)
        raise ValueError(f"Unknown ASGI server: {server!r}")


def _ensure_async_middleware(mw: object) -> None:
    """Reject sync :class:`OpMiddleware` instances at registration time.

    The whole point of the split is two parallel pipelines — silently
    treating a sync middleware as async would block the event loop.
    The :class:`AsyncOpMiddleware` Protocol is structural (it shares
    method names with the sync :class:`OpMiddleware`), so isinstance
    isn't enough — we additionally check that ``__call__`` is a
    coroutine function.
    """
    import inspect  # noqa: PLC0415

    call = getattr(type(mw), "__call__", None)  # noqa: B004 — inspecting unbound method, not testing callability
    if not inspect.iscoroutinefunction(call):
        name = type(mw).__name__
        raise TypeError(
            f"HttpAsyncApp middleware {name!r} is not an AsyncOpMiddleware "
            f"(its ``__call__`` must be ``async def``). "
            f"Use @async_op_middleware (or AsyncHttpBearerAuth / AsyncHttpBasicAuth) for the async app."
        )


# --- ASGI dispatch -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _RouteEntry:
    template: URITemplate
    methods: dict[HTTPMethod, _AsyncHandler]


@dataclass(frozen=True, slots=True)
class _MatchOk:
    template: URITemplate
    path_args: dict[str, str]
    handler: _AsyncHandler


@dataclass(frozen=True, slots=True)
class _MatchNotFound:
    pass


@dataclass(frozen=True, slots=True)
class _MatchMethodNotAllowed:
    allow: str


_MATCH_NOT_FOUND = _MatchNotFound()


class _AsgiDispatch:
    """Composed ASGI dispatcher for an :class:`HttpAsyncApp`.

    Built once per :meth:`HttpAsyncApp.asgi` call. Stores the route table
    and exposes a single ``__call__(scope, receive, send)`` that handles
    ``lifespan`` and ``http`` scopes.
    """

    __slots__ = ("_entries", "_max_body_size")

    def __init__(self, entries: tuple[_RouteEntry, ...], max_body_size: int) -> None:
        self._entries = entries
        self._max_body_size = max_body_size

    @classmethod
    def from_app(cls, app: HttpAsyncApp) -> _AsgiDispatch:
        bucket: dict[str, _RouteEntry] = {}

        def add(method: HTTPMethod, template: URITemplate, handler: _AsyncHandler) -> None:
            entry = bucket.setdefault(template.template, _RouteEntry(template=template, methods={}))
            if method in entry.methods:
                raise ValueError(f"Duplicate route: {method.value} {template.template}")
            entry.methods[method] = handler

        for op in app.operations:
            add(op.method, op.template, _async_op_handler(op))

        if app._openapi_path:
            add(
                HTTPMethod.GET,
                URITemplate.parse(app._openapi_path),
                _static_handler(app._openapi_bytes, b"application/json"),
            )
        if app._docs_path and app._openapi_path:
            ui = app._docs_ui
            html_ct = b"text/html; charset=utf-8"
            if ui in ("swagger", "all"):
                add(
                    HTTPMethod.GET,
                    URITemplate.parse(app._docs_path),
                    _static_handler(_const_bytes(swagger_html(app._openapi_path)), html_ct),
                )
            if ui in ("redoc", "all"):
                add(
                    HTTPMethod.GET,
                    URITemplate.parse(f"{app._docs_path}/redoc"),
                    _static_handler(_const_bytes(redoc_html(app._openapi_path)), html_ct),
                )
            if ui in ("scalar", "all"):
                add(
                    HTTPMethod.GET,
                    URITemplate.parse(f"{app._docs_path}/scalar"),
                    _static_handler(_const_bytes(scalar_html(app._openapi_path)), html_ct),
                )

        return cls(entries=tuple(bucket.values()), max_body_size=app._max_body_size)

    async def __call__(self, scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        kind = scope.get("type")
        if kind == "lifespan":
            await _handle_lifespan(receive, send)
            return
        if kind != "http":
            raise ValueError(f"HttpAsyncApp: unsupported ASGI scope type: {kind!r}")
        await self._handle_http(scope, receive, send)

    async def _handle_http(self, scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        path = scope["path"]
        method_str = scope["method"]
        try:
            method = HTTPMethod(method_str)
        except ValueError:
            method = None

        match = self._match(path, method)
        if isinstance(match, _MatchNotFound):
            await _send_canned(send, 404, b"Not Found")
            return
        if isinstance(match, _MatchMethodNotAllowed):
            await _send_canned(
                send,
                405,
                b"Method Not Allowed",
                extra_headers=[(b"allow", match.allow.encode("ascii"))],
            )
            return

        try:
            body = await read_body(receive, self._max_body_size)
        except ValueError:
            await _send_canned(send, 413, b"Payload Too Large")
            return

        request = build_request_from_scope(scope)
        remote, local = addrs_from_scope(scope)
        disconnected = threading.Event()
        ctx = _ASGIReqCtx(
            request=request,
            body=body,
            remote_addr=remote,
            local_addr=local,
            scheme=str(scope.get("scheme", "http")),
            _send=send,
            _disconnected=disconnected,
        )
        ctx.attrs[RouteMatch] = RouteMatch(
            method=method or HTTPMethod.GET,
            matched_template=match.template,
            path_args=match.path_args,
        )

        async with anyio.create_task_group() as tg:
            tg.start_soon(_watch_disconnect, receive, disconnected)
            try:
                await match.handler(ctx)
            finally:
                tg.cancel_scope.cancel()

    def _match(self, path: str, method: HTTPMethod | None) -> _MatchOk | _MatchNotFound | _MatchMethodNotAllowed:
        any_template_matched = False
        allow: set[HTTPMethod] = set()
        for entry in self._entries:
            args = entry.template.match(path)
            if args is None:
                continue
            any_template_matched = True
            allow.update(entry.methods)
            if method is None:
                continue
            handler = entry.methods.get(method)
            if handler is None:
                continue
            return _MatchOk(template=entry.template, path_args=args, handler=handler)
        if any_template_matched:
            return _MatchMethodNotAllowed(allow=", ".join(sorted(m.value for m in allow)))
        return _MATCH_NOT_FOUND


def _async_op_handler(op: AsyncOperation) -> _AsyncHandler:
    async def run(ctx: _ASGIReqCtx) -> None:
        await op.run(ctx)

    return run


def _static_handler(body_provider: Callable[[], bytes], content_type: bytes) -> _AsyncHandler:
    async def run(ctx: _ASGIReqCtx) -> None:
        body = body_provider()
        response = Response(
            status_code=200,
            headers=[
                (b"content-type", content_type),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        )
        await ctx.complete(response, body)

    return run


def _const_bytes(value: bytes) -> Callable[[], bytes]:
    def provider() -> bytes:
        return value

    return provider


async def _watch_disconnect(receive: ASGIReceive, flag: threading.Event) -> None:
    """Drain ASGI events while the handler runs; flip ``flag`` on
    ``http.disconnect``. Loop ends silently when the task group cancels."""
    try:
        while True:
            event = await receive()
            if event.get("type") == "http.disconnect":
                flag.set()
                return
    except Exception:  # noqa: BLE001
        # ``receive`` may raise once the response is fully sent; treat as benign.
        return


# --- ASGI lifespan / canned responses -----------------------------------


async def _handle_lifespan(receive: ASGIReceive, send: ASGISend) -> None:
    """Minimal lifespan loop — accept startup / shutdown events without
    plugging into the user's hosting service. (The hosting integration
    drives lifecycle through :mod:`localpost.hosting`.)"""
    while True:
        event = await receive()
        kind = event.get("type")
        if kind == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif kind == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return


async def _send_canned(
    send: ASGISend,
    status: int,
    body: bytes,
    *,
    extra_headers: Sequence[tuple[bytes, bytes]] = (),
) -> None:
    headers = [
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
        *extra_headers,
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})
