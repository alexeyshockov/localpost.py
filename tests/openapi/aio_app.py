"""Tests for ``localpost.openapi.HttpAsyncApp`` registration + ASGI dispatch.

Two layers of coverage:

1. **Operation-level**: feed an :class:`AsyncOperation` a fake
   :class:`AsyncHTTPReqCtx`, run it, inspect the captured response.
   Mirrors ``tests/openapi/app.py``'s sync ``run_op`` harness.
2. **ASGI-level**: drive the ASGI 3 callable from
   :meth:`HttpAsyncApp.asgi` with a mock ``receive`` / ``send`` pair —
   exercises the full route table, body buffering, and response
   translation without spinning up a real server.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from http import HTTPMethod
from typing import Annotated, Any

import pytest

from localpost.http import RouteMatch, URITemplate
from localpost.http._types import Request, Response
from localpost.openapi import (
    AsyncHttpBearerAuth,
    AsyncHTTPReqCtx,
    AsyncOpMiddleware,
    BadRequest,
    Created,
    FromHeader,
    HttpApp,
    HttpAsyncApp,
    NotFound,
    OpResult,
    async_op_middleware,
)
from localpost.openapi.aio._ctx import _ASGIReqCtx
from localpost.openapi.aio.middleware import AsyncApiOperation
from localpost.openapi.aio.operation import AsyncOperation

# --- Fakes ---------------------------------------------------------------


@dataclass(slots=True, eq=False)
class FakeAsyncCtx:
    """Minimal async ctx for unit tests — captures the response."""

    request: Request
    body: bytes = b""
    response_status: int | None = None
    attrs: dict[Any, Any] = field(default_factory=dict)
    completed: tuple[Response, bytes] | None = None
    streamed: tuple[Response, list[bytes]] | None = None

    @property
    def remote_addr(self) -> str | None:
        return None

    @property
    def local_addr(self) -> str:
        return "127.0.0.1:0"

    @property
    def scheme(self) -> str:
        return "http"

    @property
    def disconnected(self) -> bool:
        return False

    async def complete(self, response: Response, body: bytes | None = None) -> None:
        self.completed = (response, body or b"")
        self.response_status = response.status_code

    async def stream(self, response: Response, chunks: AsyncIterator[bytes], /) -> None:
        captured: list[bytes] = [bytes(chunk) async for chunk in chunks]
        self.streamed = (response, captured)
        self.response_status = response.status_code


def make_async_ctx(
    method: str = "GET",
    path: str = "/",
    query: bytes = b"",
    headers: list[tuple[bytes, bytes]] | None = None,
    body: bytes = b"",
    path_args: dict[str, str] | None = None,
    template: URITemplate | None = None,
) -> FakeAsyncCtx:
    request = Request(
        method=method.encode(),
        target=path.encode(),
        path=path.encode(),
        query_string=query,
        headers=headers or [],
        http_version=b"1.1",
    )
    ctx = FakeAsyncCtx(request=request, body=body)
    if path_args is not None:
        ctx.attrs[RouteMatch] = RouteMatch(
            method=HTTPMethod(method),
            matched_template=template or URITemplate.parse(path),
            path_args=dict(path_args),
        )
    return ctx


async def run_async_op(op: AsyncOperation, ctx: FakeAsyncCtx) -> tuple[int, bytes, dict[str, str]]:
    await op.run(ctx)
    assert ctx.completed is not None, "handler did not complete the response"
    response, body = ctx.completed
    headers = {k.decode(): v.decode("iso-8859-1") for k, v in response.headers}
    return response.status_code, body, headers


# --- Sample types --------------------------------------------------------


@dataclass
class Book:
    id: str
    title: str


# --- Registration & basic dispatch --------------------------------------


class TestRegistration:
    def test_async_handler_records_operation(self):
        app = HttpAsyncApp()

        @app.get("/foo")
        async def foo() -> str:
            return "ok"

        assert len(app.operations) == 1
        op = app.operations[0]
        assert op.method is HTTPMethod.GET
        assert op.path == "/foo"

    def test_sync_handler_rejected(self):
        app = HttpAsyncApp()

        with pytest.raises(TypeError, match="must be ``async def``"):

            @app.get("/foo")
            def foo() -> str:
                return "ok"

    def test_sync_middleware_rejected_at_construction(self):
        from localpost.openapi import OpMiddleware  # noqa: PLC0415
        from localpost.openapi.middleware import _FunctionMiddleware, op_middleware  # noqa: PLC0415

        @op_middleware
        def sync_mw(ctx, call_next) -> OpResult:  # type: ignore[no-untyped-def]
            return call_next(ctx)

        assert isinstance(sync_mw, _FunctionMiddleware)
        assert isinstance(sync_mw, OpMiddleware)

        with pytest.raises(TypeError, match="not an AsyncOpMiddleware"):
            HttpAsyncApp(middlewares=[sync_mw])  # type: ignore[list-item]

    def test_async_middleware_accepted(self):
        @async_op_middleware
        async def mw(ctx: AsyncHTTPReqCtx, call_next: AsyncApiOperation) -> OpResult:
            return await call_next(ctx)

        assert isinstance(mw, AsyncOpMiddleware)
        app = HttpAsyncApp(middlewares=[mw])
        assert len(app._middlewares) == 1


# --- Operation runtime --------------------------------------------------


class TestRuntime:
    @pytest.mark.anyio
    async def test_str_return_emits_text(self):
        app = HttpAsyncApp()

        @app.get("/hi")
        async def hi() -> str:
            return "hello"

        op = app.operations[0]
        status, body, headers = await run_async_op(op, make_async_ctx(path="/hi"))
        assert status == 200
        assert body == b"hello"
        assert headers["content-type"].startswith("text/plain")

    @pytest.mark.anyio
    async def test_dataclass_return_emits_json(self):
        app = HttpAsyncApp()

        @app.get("/b")
        async def get() -> Book:
            return Book(id="1", title="t")

        op = app.operations[0]
        status, body, headers = await run_async_op(op, make_async_ctx(path="/b"))
        assert status == 200
        assert body == b'{"id":"1","title":"t"}'
        assert headers["content-type"].startswith("application/json")

    @pytest.mark.anyio
    async def test_path_var_implicit(self):
        app = HttpAsyncApp()

        @app.get("/items/{item_id}")
        async def get_item(item_id: str) -> str:
            return item_id

        op = app.operations[0]
        ctx = make_async_ctx(path="/items/abc", path_args={"item_id": "abc"})
        status, body, _ = await run_async_op(op, ctx)
        assert status == 200
        assert body == b"abc"

    @pytest.mark.anyio
    async def test_query_param_typed(self):
        app = HttpAsyncApp()

        @app.get("/items/{item_id}")
        async def get_item(item_id: str, page: int = 1) -> str:
            return f"{item_id}/{page}"

        op = app.operations[0]
        ctx = make_async_ctx(path="/items/x", query=b"page=3", path_args={"item_id": "x"})
        status, body, _ = await run_async_op(op, ctx)
        assert status == 200
        assert body == b"x/3"

    @pytest.mark.anyio
    async def test_body_decode_dataclass(self):
        app = HttpAsyncApp()

        @app.post("/b")
        async def create(book: Book) -> Created[Book]:
            return Created(book)

        op = app.operations[0]
        ctx = make_async_ctx(method="POST", path="/b", body=b'{"id":"1","title":"t"}')
        status, body, _ = await run_async_op(op, ctx)
        assert status == 201
        assert body == b'{"id":"1","title":"t"}'

    @pytest.mark.anyio
    async def test_op_result_short_circuit(self):
        app = HttpAsyncApp()

        @app.get("/items/{item_id}")
        async def get_item(item_id: str) -> Book | NotFound[str]:
            return NotFound(f"missing {item_id}")

        op = app.operations[0]
        ctx = make_async_ctx(path="/items/x", path_args={"item_id": "x"})
        status, body, _ = await run_async_op(op, ctx)
        assert status == 404
        assert body == b"missing x"

    @pytest.mark.anyio
    async def test_resolver_validation_error_short_circuits(self):
        app = HttpAsyncApp()

        @app.get("/items/{item_id}")
        async def get_item(item_id: int) -> str:
            return str(item_id)

        op = app.operations[0]
        ctx = make_async_ctx(path="/items/x", path_args={"item_id": "x"})
        status, _, _ = await run_async_op(op, ctx)
        assert status == 400

    @pytest.mark.anyio
    async def test_async_middleware_short_circuits(self):
        @async_op_middleware
        async def block(
            ctx: AsyncHTTPReqCtx,
            call_next: AsyncApiOperation,
            x_block: Annotated[str, FromHeader("X-Block")] = "",
        ) -> BadRequest[str] | OpResult:
            if x_block == "yes":
                return BadRequest("blocked by mw")
            return await call_next(ctx)

        app = HttpAsyncApp(middlewares=[block])

        @app.get("/x")
        async def x() -> str:
            return "ok"

        op = app.operations[0]
        ctx = make_async_ctx(path="/x", headers=[(b"x-block", b"yes")])
        status, body, _ = await run_async_op(op, ctx)
        assert status == 400
        assert body == b"blocked by mw"

    @pytest.mark.anyio
    async def test_async_middleware_passthrough(self):
        @async_op_middleware
        async def passthrough(ctx: AsyncHTTPReqCtx, call_next: AsyncApiOperation) -> OpResult:
            return await call_next(ctx)

        app = HttpAsyncApp(middlewares=[passthrough])

        @app.get("/x")
        async def x() -> str:
            return "ok"

        op = app.operations[0]
        status, body, _ = await run_async_op(op, make_async_ctx(path="/x"))
        assert status == 200
        assert body == b"ok"


# --- SSE -----------------------------------------------------------------


class TestSSE:
    @pytest.mark.anyio
    async def test_async_generator_streams_events(self):
        app = HttpAsyncApp()

        @app.get("/events")
        async def events() -> AsyncIterator[str]:
            for i in range(3):
                yield f"tick-{i}"

        op = app.operations[0]
        ctx = make_async_ctx(path="/events")
        await op.run(ctx)
        assert ctx.streamed is not None
        response, chunks = ctx.streamed
        assert response.status_code == 200
        ct = next(v for k, v in response.headers if k == b"content-type")
        assert ct.startswith(b"text/event-stream")
        wire = b"".join(chunks)
        assert b"data: tick-0\n\n" in wire
        assert b"data: tick-2\n\n" in wire

    @pytest.mark.anyio
    async def test_sync_generator_rejected(self):
        app = HttpAsyncApp()

        @app.get("/bad")
        async def bad():
            def sync_gen():
                yield "x"

            return sync_gen()

        op = app.operations[0]
        with pytest.raises(TypeError, match="sync iterator/generator"):
            await op.run(make_async_ctx(path="/bad"))


# --- OpenAPI doc parity --------------------------------------------------


class TestSpecParity:
    def test_async_app_doc_matches_sync_for_same_routes(self):
        # Same routes registered on both flavours should produce the same
        # operationId / responses / parameters in the OpenAPI doc.
        sync_app = HttpApp()
        async_app = HttpAsyncApp()

        @sync_app.get("/items/{item_id}")
        def get_item_sync(item_id: str) -> Book | NotFound[str]:
            return NotFound("nope")

        @async_app.get("/items/{item_id}")
        async def get_item_async(item_id: str) -> Book | NotFound[str]:
            return NotFound("nope")

        sync_doc = sync_app.openapi_doc
        async_doc = async_app.openapi_doc
        s_op = sync_doc.paths["/items/{item_id}"].operations["get"]
        a_op = async_doc.paths["/items/{item_id}"].operations["get"]
        assert sorted(s_op.responses) == sorted(a_op.responses)
        assert [p.name for p in s_op.parameters] == [p.name for p in a_op.parameters]


# --- ASGI dispatch -------------------------------------------------------


async def _drive_asgi(
    asgi_app: Any,
    method: str,
    path: str,
    *,
    body: bytes = b"",
    headers: list[tuple[bytes, bytes]] | None = None,
    query: bytes = b"",
) -> tuple[int, bytes, list[tuple[bytes, bytes]]]:
    """Drive a single HTTP request through an ASGI app and capture the response."""
    scope: dict[str, Any] = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": query,
        "headers": headers or [],
        "scheme": "http",
        "http_version": "1.1",
        "client": ["127.0.0.1", 12345],
        "server": ["127.0.0.1", 8000],
    }
    request_done = False

    async def receive() -> dict[str, Any]:
        nonlocal request_done
        if not request_done:
            request_done = True
            return {"type": "http.request", "body": body, "more_body": False}
        # After the body is delivered, hold open until the response is sent.
        # The dispatcher cancels the watcher task once the handler returns.
        await _forever()
        raise AssertionError("unreachable")

    captured_status: list[int] = []
    captured_headers: list[list[tuple[bytes, bytes]]] = []
    captured_body = bytearray()

    async def send(event: dict[str, Any]) -> None:
        kind = event["type"]
        if kind == "http.response.start":
            captured_status.append(event["status"])
            captured_headers.append(list(event["headers"]))
        elif kind == "http.response.body":
            captured_body.extend(event.get("body", b""))

    await asgi_app(scope, receive, send)
    assert captured_status, "handler did not start the response"
    return captured_status[0], bytes(captured_body), captured_headers[0]


async def _forever() -> None:
    import anyio  # noqa: PLC0415

    await anyio.sleep_forever()


class TestAsgiDispatch:
    @pytest.mark.anyio
    async def test_simple_get(self):
        app = HttpAsyncApp()

        @app.get("/hello/{name}")
        async def hello(name: str) -> str:
            return f"hi {name}"

        asgi_app = app.asgi()
        status, body, _ = await _drive_asgi(asgi_app, "GET", "/hello/world")
        assert status == 200
        assert body == b"hi world"

    @pytest.mark.anyio
    async def test_404(self):
        app = HttpAsyncApp()

        asgi_app = app.asgi()
        status, body, _ = await _drive_asgi(asgi_app, "GET", "/nope")
        assert status == 404
        assert body == b"Not Found"

    @pytest.mark.anyio
    async def test_405_method_not_allowed(self):
        app = HttpAsyncApp()

        @app.get("/foo")
        async def foo() -> str:
            return "ok"

        asgi_app = app.asgi()
        status, _, headers = await _drive_asgi(asgi_app, "POST", "/foo")
        assert status == 405
        allow = next(v for k, v in headers if k == b"allow")
        assert b"GET" in allow

    @pytest.mark.anyio
    async def test_post_with_body(self):
        app = HttpAsyncApp()

        @app.post("/b")
        async def create(book: Book) -> Created[Book]:
            return Created(book)

        asgi_app = app.asgi()
        status, body, _ = await _drive_asgi(asgi_app, "POST", "/b", body=b'{"id":"1","title":"t"}')
        assert status == 201
        assert body == b'{"id":"1","title":"t"}'

    @pytest.mark.anyio
    async def test_openapi_endpoint(self):
        app = HttpAsyncApp()

        @app.get("/x")
        async def x() -> str:
            return "ok"

        asgi_app = app.asgi()
        status, body, headers = await _drive_asgi(asgi_app, "GET", "/openapi.json")
        assert status == 200
        ct = next(v for k, v in headers if k == b"content-type")
        assert ct == b"application/json"
        assert b'"openapi"' in body

    @pytest.mark.anyio
    async def test_docs_endpoint(self):
        app = HttpAsyncApp()
        asgi_app = app.asgi()
        status, body, headers = await _drive_asgi(asgi_app, "GET", "/docs")
        assert status == 200
        ct = next(v for k, v in headers if k == b"content-type")
        assert ct.startswith(b"text/html")
        assert b"swagger" in body.lower()

    @pytest.mark.anyio
    async def test_payload_too_large(self):
        app = HttpAsyncApp(max_body_size=8)

        @app.post("/b")
        async def create(book: Book) -> Created[Book]:
            return Created(book)

        asgi_app = app.asgi()
        status, _, _ = await _drive_asgi(asgi_app, "POST", "/b", body=b'{"id":"1","title":"way-too-long"}')
        assert status == 413


# --- Async auth ---------------------------------------------------------


class TestAsyncAuth:
    @pytest.mark.anyio
    async def test_bearer_valid_token(self):
        bearer = AsyncHttpBearerAuth(validator=lambda t: {"sub": t} if t == "good" else None)
        app = HttpAsyncApp(middlewares=[bearer])

        @app.get("/me")
        async def me() -> str:
            return "hello"

        op = app.operations[0]
        ctx = make_async_ctx(path="/me", headers=[(b"authorization", b"Bearer good")])
        status, body, _ = await run_async_op(op, ctx)
        assert status == 200
        assert body == b"hello"

    @pytest.mark.anyio
    async def test_bearer_invalid_token(self):
        bearer = AsyncHttpBearerAuth(validator=lambda _t: None)
        app = HttpAsyncApp(middlewares=[bearer])

        @app.get("/me")
        async def me() -> str:
            return "hello"

        op = app.operations[0]
        ctx = make_async_ctx(path="/me", headers=[(b"authorization", b"Bearer bad")])
        status, _, _ = await run_async_op(op, ctx)
        assert status == 401

    @pytest.mark.anyio
    async def test_bearer_async_validator(self):
        async def validate(t: str) -> dict[str, str] | None:
            return {"sub": t} if t == "good" else None

        bearer = AsyncHttpBearerAuth(validator=validate)
        app = HttpAsyncApp(middlewares=[bearer])

        @app.get("/me")
        async def me() -> str:
            return "hello"

        op = app.operations[0]
        ctx = make_async_ctx(path="/me", headers=[(b"authorization", b"Bearer good")])
        status, _, _ = await run_async_op(op, ctx)
        assert status == 200

    def test_bearer_registers_security_scheme(self):
        bearer = AsyncHttpBearerAuth(validator=lambda _t: None)
        app = HttpAsyncApp(middlewares=[bearer])

        @app.get("/me")
        async def me() -> str:
            return "hi"

        doc = app.openapi_doc
        assert "bearerAuth" in doc.components.security_schemes
        op_spec = doc.paths["/me"].operations["get"]
        assert {"bearerAuth": ()} in op_spec.security


# --- ASGI ctx unit tests ------------------------------------------------


class TestAsgiCtxUnit:
    @pytest.mark.anyio
    async def test_complete_emits_start_then_body(self):
        events: list[dict[str, Any]] = []

        async def send(event: dict[str, Any]) -> None:
            events.append(event)

        ctx = _ASGIReqCtx(
            request=Request(b"GET", b"/", b"/", b"", []),
            body=b"",
            remote_addr=None,
            local_addr="0.0.0.0:0",
            scheme="http",
            _send=send,
            _disconnected=threading.Event(),
        )
        await ctx.complete(Response(status_code=200, headers=[(b"x-y", b"z")]), b"hello")
        assert [e["type"] for e in events] == ["http.response.start", "http.response.body"]
        assert events[0]["status"] == 200
        assert (b"x-y", b"z") in events[0]["headers"]
        assert events[1]["body"] == b"hello"
        assert events[1]["more_body"] is False

    @pytest.mark.anyio
    async def test_complete_twice_raises(self):
        async def send(_event: dict[str, Any]) -> None:
            pass

        ctx = _ASGIReqCtx(
            request=Request(b"GET", b"/", b"/", b"", []),
            body=b"",
            remote_addr=None,
            local_addr="0.0.0.0:0",
            scheme="http",
            _send=send,
            _disconnected=threading.Event(),
        )
        await ctx.complete(Response(200), b"x")
        with pytest.raises(RuntimeError, match="already started"):
            await ctx.complete(Response(200), b"y")
