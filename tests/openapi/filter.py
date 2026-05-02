"""Tests for ``OpFilter`` wiring through ``HttpApp`` and ``Operation``."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from localpost.openapi import HttpApp, OpFilter, Unauthorized, spec
from tests.openapi.app import make_ctx, run_op

if TYPE_CHECKING:
    from localpost.http.server import HTTPReqCtx
    from localpost.openapi.results import OpResult
    from localpost.openapi.schemas import SchemaRegistry


# --- Test filter helpers -------------------------------------------------


class _RecordingFilter:
    """Filter that just records calls; never short-circuits."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, ctx: HTTPReqCtx, /) -> None | OpResult:
        self.calls.append("run")
        return None

    def contribute_root(self, doc: spec.OpenAPI, registry: SchemaRegistry, /) -> spec.OpenAPI:
        self.calls.append("contribute_root")
        return doc

    def contribute_operation(self, op: spec.Operation, registry: SchemaRegistry, /) -> spec.Operation:
        self.calls.append("contribute_operation")
        return op


class _DenyFilter:
    """Filter that always short-circuits with 401."""

    def __init__(self, message: str = "denied") -> None:
        self.message = message

    def __call__(self, ctx: HTTPReqCtx, /) -> None | OpResult:
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


class TestFilterIsRuntimeProtocol:
    def test_recording_satisfies_protocol(self):
        assert isinstance(_RecordingFilter(), OpFilter)

    def test_deny_satisfies_protocol(self):
        assert isinstance(_DenyFilter(), OpFilter)


class TestAppLevelFilterRuntime:
    def test_filter_runs_before_handler(self):
        rec = _RecordingFilter()
        app = HttpApp(filters=[rec])

        @app.get("/foo")
        def foo() -> str:
            return "ok"

        op = app.operations[0]
        ctx = make_ctx(path="/foo")
        status, body, _ = run_op(op, ctx)
        assert status == 200
        assert body == b"ok"
        # The filter ran for the request, plus contribute_operation at registration.
        # contribute_root only fires when openapi_doc is built.
        assert rec.calls == ["run"]

    def test_filter_short_circuits(self):
        app = HttpApp(filters=[_DenyFilter("nope")])

        @app.get("/foo")
        def foo() -> str:
            return "ok"

        op = app.operations[0]
        ctx = make_ctx(path="/foo")
        status, body, _ = run_op(op, ctx)
        assert status == 401
        assert body == b"nope"


class TestPerOpFilter:
    def test_per_op_filter_only_affects_that_op(self):
        deny = _DenyFilter("op-only")
        app = HttpApp()

        @app.get("/protected", filters=[deny])
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

    def test_app_and_per_op_filters_compose(self):
        order: list[str] = []

        class _Tagging:
            def __init__(self, tag: str) -> None:
                self.tag = tag

            def __call__(self, ctx: HTTPReqCtx, /) -> None | OpResult:
                order.append(self.tag)
                return None

            def contribute_root(self, doc, registry):  # noqa: ANN001
                return doc

            def contribute_operation(self, op, registry):  # noqa: ANN001
                return op

        app = HttpApp(filters=[_Tagging("app")])

        @app.get("/foo", filters=[_Tagging("op")])
        def foo() -> str:
            return "ok"

        run_op(app.operations[0], make_ctx(path="/foo"))
        # App-level runs before per-op.
        assert order == ["app", "op"]


class TestSpecContribution:
    def test_contribute_root_called_once_per_doc_build(self):
        rec = _RecordingFilter()
        app = HttpApp(filters=[rec])

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
        app = HttpApp(filters=[_DenyFilter()])

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
        app = HttpApp(filters=[_DenyFilter()])

        @app.get("/foo")
        def foo() -> str:
            return "ok"

        doc = app.openapi_doc.to_dict()
        op = doc["paths"]["/foo"]["get"]
        assert op["security"] == [{"denyAuth": []}]
        assert "401" in op["responses"]
        assert op["responses"]["401"] == {"description": "Unauthorized"}

    def test_per_op_filter_doc_contribution_is_op_scoped(self):
        app = HttpApp()

        @app.get("/protected", filters=[_DenyFilter()])
        def protected() -> str:
            return "secret"

        @app.get("/public")
        def public() -> str:
            return "hi"

        doc = app.openapi_doc.to_dict()
        # Per-op filter contributes to BOTH root (because contribute_root
        # is always called once per filter that sees the doc) and only to
        # its own operation.
        assert "denyAuth" in doc["components"]["securitySchemes"]
        assert "security" in doc["paths"]["/protected"]["get"]
        assert "security" not in doc["paths"]["/public"]["get"]
        assert "401" not in doc["paths"]["/public"]["get"]["responses"]
