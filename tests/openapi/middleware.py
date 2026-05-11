"""Tests for ``OpMiddleware`` wiring through ``HttpApp`` and ``Operation``,
plus the ``@op_middleware`` decorator."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Annotated

import pytest

from localpost.http import HTTPReqCtx
from localpost.openapi import (
    ApiOperation,
    BadRequest,
    FromHeader,
    FromQuery,
    HttpApp,
    Ok,
    OpMiddleware,
    OpResult,
    TooManyRequests,
    Unauthorized,
    op_middleware,
    spec,
)
from tests.openapi.app import make_ctx, run_op

if TYPE_CHECKING:
    from localpost.openapi.schemas import SchemaRegistry


# --- Test middleware helpers ---------------------------------------------


class _RecordingMiddleware:
    """Middleware that just records calls; always forwards via ``call_next``."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, ctx: HTTPReqCtx, call_next: ApiOperation, /) -> OpResult:
        self.calls.append("run")
        return call_next(ctx)

    def contribute_root(self, doc: spec.OpenAPI, registry: SchemaRegistry, /) -> spec.OpenAPI:
        self.calls.append("contribute_root")
        return doc

    def contribute_operation(self, op: spec.Operation, registry: SchemaRegistry, /) -> spec.Operation:
        self.calls.append("contribute_operation")
        return op


class _DenyMiddleware:
    """Middleware that always short-circuits with 401."""

    def __init__(self, message: str = "denied") -> None:
        self.message = message

    def __call__(self, ctx: HTTPReqCtx, call_next: ApiOperation, /) -> OpResult:
        return Unauthorized(self.message)

    def contribute_root(self, doc: spec.OpenAPI, registry: SchemaRegistry, /) -> spec.OpenAPI:
        new_components = replace(
            doc.components,
            security_schemes={
                **doc.components.security_schemes,
                "denyAuth": spec.SecurityScheme(type="http", scheme="bearer"),
            },
        )
        return replace(doc, components=new_components)

    def contribute_operation(self, op: spec.Operation, registry: SchemaRegistry, /) -> spec.Operation:
        return replace(
            op,
            security=(*op.security, {"denyAuth": ()}),
            responses={**op.responses, "401": spec.Response(description="Unauthorized")},
        )


class TestMiddlewareIsRuntimeProtocol:
    def test_recording_satisfies_protocol(self):
        assert isinstance(_RecordingMiddleware(), OpMiddleware)

    def test_deny_satisfies_protocol(self):
        assert isinstance(_DenyMiddleware(), OpMiddleware)


class TestAppLevelMiddlewareRuntime:
    def test_middleware_runs_around_handler(self):
        rec = _RecordingMiddleware()
        app = HttpApp(middlewares=[rec])

        @app.get("/foo")
        def foo() -> str:
            return "ok"

        op = app.operations[0]
        ctx = make_ctx(path="/foo")
        status, body, _ = run_op(op, ctx)
        assert status == 200
        assert body == b"ok"
        # The middleware ran for the request.
        assert rec.calls == ["run"]

    def test_middleware_short_circuits(self):
        app = HttpApp(middlewares=[_DenyMiddleware("nope")])

        @app.get("/foo")
        def foo() -> str:
            return "ok"

        op = app.operations[0]
        ctx = make_ctx(path="/foo")
        status, body, _ = run_op(op, ctx)
        assert status == 401
        assert body == b"nope"

    def test_middleware_can_post_process_op_result(self):
        class _AddHeader:
            def __call__(self, ctx: HTTPReqCtx, call_next: ApiOperation, /) -> OpResult:
                result = call_next(ctx)
                # Re-wrap with extra headers. (replace() doesn't work on
                # OpResult subclasses because their __init__ is custom.)
                return Ok(result.body, headers={**result.headers, "X-Trace": "abc"})

            def contribute_root(self, doc: spec.OpenAPI, registry: SchemaRegistry, /) -> spec.OpenAPI:
                return doc

            def contribute_operation(self, op: spec.Operation, registry: SchemaRegistry, /) -> spec.Operation:
                return op

        app = HttpApp(middlewares=[_AddHeader()])

        @app.get("/foo")
        def foo() -> str:
            return "ok"

        op = app.operations[0]
        status, _, headers = run_op(op, make_ctx(path="/foo"))
        assert status == 200
        assert headers["X-Trace"] == "abc"


class TestPerOpMiddleware:
    def test_per_op_middleware_only_affects_that_op(self):
        deny = _DenyMiddleware("op-only")
        app = HttpApp()

        @app.get("/protected", middlewares=[deny])
        def protected() -> str:
            return "secret"

        @app.get("/public")
        def public() -> str:
            return "hi"

        protected_op = app.operations[0]
        public_op = app.operations[1]

        status, _, _ = run_op(protected_op, make_ctx(path="/protected"))
        assert status == 401

        status, body, _ = run_op(public_op, make_ctx(path="/public"))
        assert status == 200
        assert body == b"hi"

    def test_app_and_per_op_middlewares_compose(self):
        order: list[str] = []

        class _Tagging:
            def __init__(self, tag: str) -> None:
                self.tag = tag

            def __call__(self, ctx: HTTPReqCtx, call_next: ApiOperation, /) -> OpResult:
                order.append(self.tag)
                return call_next(ctx)

            def contribute_root(self, doc: spec.OpenAPI, registry: SchemaRegistry, /) -> spec.OpenAPI:
                return doc

            def contribute_operation(self, op: spec.Operation, registry: SchemaRegistry, /) -> spec.Operation:
                return op

        app = HttpApp(middlewares=[_Tagging("app")])

        @app.get("/foo", middlewares=[_Tagging("op")])
        def foo() -> str:
            return "ok"

        run_op(app.operations[0], make_ctx(path="/foo"))
        # App-level wraps outermost; per-op runs inside it.
        assert order == ["app", "op"]


class TestSpecContribution:
    def test_contribute_root_called_once_per_doc_build(self):
        rec = _RecordingMiddleware()
        app = HttpApp(middlewares=[rec])

        @app.get("/a")
        def a() -> str:
            return "a"

        @app.get("/b")
        def b() -> str:
            return "b"

        rec.calls.clear()
        _ = app.openapi_doc
        # contribute_root once, contribute_operation per operation.
        assert rec.calls.count("contribute_root") == 1
        assert rec.calls.count("contribute_operation") == 2

    def test_contribute_root_appears_in_doc(self):
        app = HttpApp(middlewares=[_DenyMiddleware()])

        @app.get("/foo")
        def foo() -> str:
            return "ok"

        doc = app.openapi_doc.to_dict()
        assert "denyAuth" in doc["components"]["securitySchemes"]
        assert doc["components"]["securitySchemes"]["denyAuth"] == {
            "type": "http",
            "scheme": "bearer",
        }

    def test_contribute_operation_appears_in_doc(self):
        app = HttpApp(middlewares=[_DenyMiddleware()])

        @app.get("/foo")
        def foo() -> str:
            return "ok"

        doc = app.openapi_doc.to_dict()
        op = doc["paths"]["/foo"]["get"]
        assert op["security"] == [{"denyAuth": []}]
        assert "401" in op["responses"]
        assert op["responses"]["401"] == {"description": "Unauthorized"}

    def test_per_op_middleware_doc_contribution_is_op_scoped(self):
        app = HttpApp()

        @app.get("/protected", middlewares=[_DenyMiddleware()])
        def protected() -> str:
            return "secret"

        @app.get("/public")
        def public() -> str:
            return "hi"

        doc = app.openapi_doc.to_dict()
        # Per-op middleware contributes to BOTH root (because contribute_root
        # is always called once per middleware that sees the doc) and only
        # to its own operation.
        assert "denyAuth" in doc["components"]["securitySchemes"]
        assert "security" in doc["paths"]["/protected"]["get"]
        assert "security" not in doc["paths"]["/public"]["get"]
        assert "401" not in doc["paths"]["/public"]["get"]["responses"]


# --- @op_middleware decorator ------------------------------------------


class TestOpMiddlewareDecorator:
    def test_decorator_produces_middleware(self):
        @op_middleware
        def f(ctx: HTTPReqCtx, call_next: ApiOperation) -> OpResult:
            return call_next(ctx)

        assert isinstance(f, OpMiddleware)

    def test_missing_call_next_param_is_error(self):
        with pytest.raises(ValueError, match="call_next"):

            @op_middleware
            def bad() -> OpResult:  # type: ignore[empty-body]
                ...


class TestOpMiddlewareRuntime:
    def test_resolved_args_are_available(self):
        seen: list[str] = []

        @op_middleware
        def record(
            ctx: HTTPReqCtx,
            call_next: ApiOperation,
            authorization: Annotated[str, FromHeader("Authorization")],
        ) -> OpResult:
            seen.append(authorization)
            return call_next(ctx)

        app = HttpApp(middlewares=[record])

        @app.get("/x")
        def x() -> str:
            return "ok"

        op = app.operations[0]
        ctx = make_ctx(path="/x", headers=[(b"authorization", b"Bearer abc")])
        status, _, _ = run_op(op, ctx)
        assert status == 200
        assert seen == ["Bearer abc"]

    def test_short_circuit_with_op_result(self):
        @op_middleware
        def deny(
            ctx: HTTPReqCtx,
            call_next: ApiOperation,
            code: Annotated[str, FromQuery("code")] = "",
        ) -> Unauthorized[str] | OpResult:
            if code != "good":
                return Unauthorized("Invalid code")
            return call_next(ctx)

        app = HttpApp(middlewares=[deny])

        @app.get("/x")
        def x() -> str:
            return "secret"

        op = app.operations[0]
        status, body, _ = run_op(op, make_ctx(path="/x", query=b"code=bad"))
        assert status == 401
        assert body == b"Invalid code"

    def test_short_circuit_via_resolver(self):
        # When a resolver itself returns an OpResult (e.g. missing required
        # header → BadRequest), the middleware short-circuits with that
        # result without calling the inner chain.
        @op_middleware
        def needs_header(
            ctx: HTTPReqCtx,
            call_next: ApiOperation,
            x_id: Annotated[str, FromHeader("X-Id")],
        ) -> OpResult:
            return call_next(ctx)

        app = HttpApp(middlewares=[needs_header])

        @app.get("/x")
        def x() -> str:
            return "ok"

        op = app.operations[0]
        # No X-Id header → resolver short-circuits with 400.
        status, body, _ = run_op(op, make_ctx(path="/x"))
        assert status == 400
        assert b"Missing required header" in body

    def test_non_op_result_return_is_error(self):
        @op_middleware
        def bad(ctx: HTTPReqCtx, call_next: ApiOperation) -> OpResult:
            return "not allowed"  # type: ignore[return-value]

        app = HttpApp(middlewares=[bad])

        @app.get("/x")
        def x() -> str:
            return "ok"

        op = app.operations[0]
        with pytest.raises(TypeError, match="middlewares must return an OpResult"):
            run_op(op, make_ctx(path="/x"))


class TestOpMiddlewareSpecContribution:
    def test_header_param_appears_on_operation(self):
        @op_middleware
        def require_header(
            ctx: HTTPReqCtx,
            call_next: ApiOperation,
            x_api_key: Annotated[str, FromHeader("X-API-Key")],
        ) -> Unauthorized[str] | OpResult:
            return call_next(ctx) if x_api_key else Unauthorized("missing")

        app = HttpApp(middlewares=[require_header])

        @app.get("/x")
        def x() -> str:
            return "ok"

        params = app.openapi_doc.to_dict()["paths"]["/x"]["get"]["parameters"]
        names = [p["name"] for p in params]
        assert "X-API-Key" in names
        # Coming from the middleware, the param is required.
        x_api_key_param = next(p for p in params if p["name"] == "X-API-Key")
        assert x_api_key_param.get("required") is True

    def test_query_param_appears_on_operation(self):
        @op_middleware
        def require_token(
            ctx: HTTPReqCtx,
            call_next: ApiOperation,
            token: Annotated[str, FromQuery("token")],
        ) -> Unauthorized[str] | OpResult:
            return call_next(ctx) if token else Unauthorized("missing")

        app = HttpApp(middlewares=[require_token])

        @app.get("/x")
        def x() -> str:
            return "ok"

        params = app.openapi_doc.to_dict()["paths"]["/x"]["get"]["parameters"]
        names = [p["name"] for p in params]
        assert "token" in names

    def test_response_codes_from_return_type_union(self):
        @op_middleware
        def rate_limit(
            ctx: HTTPReqCtx,
            call_next: ApiOperation,
        ) -> TooManyRequests[str] | OpResult:
            return call_next(ctx)

        app = HttpApp(middlewares=[rate_limit])

        @app.get("/x")
        def x() -> str:
            return "ok"

        responses = app.openapi_doc.to_dict()["paths"]["/x"]["get"]["responses"]
        assert "429" in responses
        assert responses["429"]["description"] == "Too Many Requests"

    def test_middleware_response_does_not_overwrite_operation_response(self):
        # If the operation already declares 400 for its own reason, the
        # middleware's 400 (if any) shouldn't clobber it.
        @op_middleware
        def always_pass(
            ctx: HTTPReqCtx,
            call_next: ApiOperation,
        ) -> BadRequest[int] | OpResult:  # different body type
            return call_next(ctx)

        app = HttpApp(middlewares=[always_pass])

        @app.get("/x")
        def x() -> str | BadRequest[str]:  # the op's own 400 — wins
            return "ok"

        responses = app.openapi_doc.to_dict()["paths"]["/x"]["get"]["responses"]
        assert "400" in responses
        # The op's BadRequest[str] wins over the middleware's BadRequest[int].
        assert responses["400"]["content"]["application/json"]["schema"] == {"type": "string"}

    def test_per_op_middleware_only_contributes_to_its_op(self):
        @op_middleware
        def deny(
            ctx: HTTPReqCtx,
            call_next: ApiOperation,
            code: Annotated[str, FromQuery("code")] = "",
        ) -> Unauthorized[str] | OpResult:
            return Unauthorized("nope") if code != "good" else call_next(ctx)

        app = HttpApp()

        @app.get("/protected", middlewares=[deny])
        def protected() -> str:
            return "x"

        @app.get("/public")
        def public() -> str:
            return "y"

        doc = app.openapi_doc.to_dict()
        # /protected has the middleware's parameter and 401 response
        assert any(p["name"] == "code" for p in doc["paths"]["/protected"]["get"]["parameters"])
        assert "401" in doc["paths"]["/protected"]["get"]["responses"]
        # /public doesn't
        assert "parameters" not in doc["paths"]["/public"]["get"]
        assert "401" not in doc["paths"]["/public"]["get"]["responses"]


class TestCtxParam:
    def test_ctx_param_is_passthrough_and_contributes_nothing(self):
        seen: list[str] = []

        @op_middleware
        def stash(ctx: HTTPReqCtx, call_next: ApiOperation) -> OpResult:
            seen.append(ctx.request.path.decode())
            return call_next(ctx)

        app = HttpApp(middlewares=[stash])

        @app.get("/x")
        def x() -> str:
            return "ok"

        op = app.operations[0]
        run_op(op, make_ctx(path="/x"))
        assert seen == ["/x"]

        # ctx parameter does NOT show up in the spec.
        op_spec = app.openapi_doc.to_dict()["paths"]["/x"]["get"]
        assert "parameters" not in op_spec
