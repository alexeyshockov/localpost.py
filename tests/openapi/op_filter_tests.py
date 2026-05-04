"""Tests for the ``@op_filter`` decorator."""

from __future__ import annotations

from typing import Annotated

from localpost.http import HTTPReqCtx
from localpost.openapi import (
    BadRequest,
    FromHeader,
    FromQuery,
    HttpApp,
    OpFilter,
    TooManyRequests,
    Unauthorized,
    op_filter,
)
from tests.openapi.app import make_ctx, run_op


# --- Wrapper smoke -------------------------------------------------------


class TestProtocolConformance:
    def test_decorator_produces_op_filter(self):
        @op_filter
        def f() -> None:
            return None

        assert isinstance(f, OpFilter)


# --- Runtime semantics ---------------------------------------------------


class TestRuntime:
    def test_filter_runs_resolved_args(self):
        seen: list[str] = []

        @op_filter
        def record(authorization: Annotated[str, FromHeader("Authorization")]) -> None:
            seen.append(authorization)
            return None

        app = HttpApp(filters=[record])

        @app.get("/x")
        def x() -> str:
            return "ok"

        op = app.operations[0]
        ctx = make_ctx(path="/x", headers=[(b"authorization", b"Bearer abc")])
        status, _, _ = run_op(op, ctx)
        assert status == 200
        assert seen == ["Bearer abc"]

    def test_filter_short_circuit_with_op_result(self):
        @op_filter
        def deny(token: Annotated[str, FromQuery("token")] = "") -> None | Unauthorized[str]:
            if token != "good":
                return Unauthorized("Invalid token")
            return None

        app = HttpApp(filters=[deny])

        @app.get("/x")
        def x() -> str:
            return "secret"

        op = app.operations[0]
        status, body, _ = run_op(op, make_ctx(path="/x", query=b"token=bad"))
        assert status == 401
        assert body == b"Invalid token"

    def test_filter_short_circuit_via_resolver(self):
        # When a resolver itself returns an OpResult (e.g. missing required
        # header → BadRequest), the filter short-circuits with that result.
        @op_filter
        def needs_header(x_id: Annotated[str, FromHeader("X-Id")]) -> None:
            return None

        app = HttpApp(filters=[needs_header])

        @app.get("/x")
        def x() -> str:
            return "ok"

        op = app.operations[0]
        # No X-Id header → resolver short-circuits with 400.
        status, body, _ = run_op(op, make_ctx(path="/x"))
        assert status == 400
        assert b"Missing required header" in body

    def test_non_none_non_op_result_return_is_error(self):
        import pytest

        @op_filter
        def bad() -> str:
            return "not allowed"  # type: ignore[return-value]

        app = HttpApp(filters=[bad])

        @app.get("/x")
        def x() -> str:
            return "ok"

        op = app.operations[0]
        with pytest.raises(TypeError, match="filters must return None"):
            run_op(op, make_ctx(path="/x"))


# --- Spec contribution ---------------------------------------------------


class TestSpecContribution:
    def test_header_param_appears_on_operation(self):
        @op_filter
        def require_header(
            x_api_key: Annotated[str, FromHeader("X-API-Key")],
        ) -> None | Unauthorized[str]:
            return None if x_api_key else Unauthorized("missing")

        app = HttpApp(filters=[require_header])

        @app.get("/x")
        def x() -> str:
            return "ok"

        params = app.openapi_doc.to_dict()["paths"]["/x"]["get"]["parameters"]
        names = [p["name"] for p in params]
        assert "X-API-Key" in names
        # Coming from the filter, the param is required.
        x_api_key_param = next(p for p in params if p["name"] == "X-API-Key")
        assert x_api_key_param.get("required") is True

    def test_query_param_appears_on_operation(self):
        @op_filter
        def require_token(token: Annotated[str, FromQuery("token")]) -> None | Unauthorized[str]:
            return None if token else Unauthorized("missing")

        app = HttpApp(filters=[require_token])

        @app.get("/x")
        def x() -> str:
            return "ok"

        params = app.openapi_doc.to_dict()["paths"]["/x"]["get"]["parameters"]
        names = [p["name"] for p in params]
        assert "token" in names

    def test_response_codes_from_return_type_union(self):
        @op_filter
        def rate_limit() -> None | TooManyRequests[str]:
            return None

        app = HttpApp(filters=[rate_limit])

        @app.get("/x")
        def x() -> str:
            return "ok"

        responses = app.openapi_doc.to_dict()["paths"]["/x"]["get"]["responses"]
        assert "429" in responses
        assert responses["429"]["description"] == "Too Many Requests"

    def test_filter_response_does_not_overwrite_operation_response(self):
        # If the operation already declares 400 for its own reason, the
        # filter's 400 (if any) shouldn't clobber it.
        @op_filter
        def always_pass() -> None | BadRequest[int]:  # different body type
            return None

        app = HttpApp(filters=[always_pass])

        @app.get("/x")
        def x() -> str | BadRequest[str]:  # the op's own 400 — wins
            return "ok"

        responses = app.openapi_doc.to_dict()["paths"]["/x"]["get"]["responses"]
        assert "400" in responses
        # The op's BadRequest[str] wins over the filter's BadRequest[int].
        assert responses["400"]["content"]["application/json"]["schema"] == {"type": "string"}

    def test_per_op_filter_only_contributes_to_its_op(self):
        @op_filter
        def deny(token: Annotated[str, FromQuery("token")] = "") -> None | Unauthorized[str]:
            return Unauthorized("nope") if token != "good" else None

        app = HttpApp()

        @app.get("/protected", filters=[deny])
        def protected() -> str:
            return "x"

        @app.get("/public")
        def public() -> str:
            return "y"

        doc = app.openapi_doc.to_dict()
        # /protected has the filter's parameter and 401 response
        assert any(p["name"] == "token" for p in doc["paths"]["/protected"]["get"]["parameters"])
        assert "401" in doc["paths"]["/protected"]["get"]["responses"]
        # /public doesn't
        assert "parameters" not in doc["paths"]["/public"]["get"]
        assert "401" not in doc["paths"]["/public"]["get"]["responses"]


# --- Compose with HTTPReqCtx --------------------------------------------


class TestCtxParam:
    def test_ctx_param_is_passthrough_and_contributes_nothing(self):
        seen: list[str] = []

        @op_filter
        def stash(ctx: HTTPReqCtx) -> None:
            seen.append(ctx.request.path.decode())
            return None

        app = HttpApp(filters=[stash])

        @app.get("/x")
        def x() -> str:
            return "ok"

        op = app.operations[0]
        run_op(op, make_ctx(path="/x"))
        assert seen == ["/x"]

        # ctx parameter does NOT show up in the spec.
        op_spec = app.openapi_doc.to_dict()["paths"]["/x"]["get"]
        assert "parameters" not in op_spec
