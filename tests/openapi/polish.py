"""Tests for the resolver / Operation polish: Optional handling,
operationId derivation, registration-time validation."""

from __future__ import annotations

from typing import Annotated

import msgspec
import pytest

from localpost.openapi import FromHeader, FromPath, FromQuery, HttpApp
from tests.openapi.app import make_ctx, run_op


# --- Optional handling ---------------------------------------------------


class TestOptionalQuery:
    def test_optional_query_param_returns_none_when_absent(self):
        app = HttpApp()

        @app.get("/items")
        def items(q: str | None = None) -> str:
            return "missing" if q is None else q

        op = app.operations[0]
        status, body, _ = run_op(op, make_ctx(path="/items"))
        assert status == 200
        assert body == b"missing"

    def test_optional_via_union_without_default(self):
        app = HttpApp()

        @app.get("/items")
        def items(q: str | None) -> str:
            return "missing" if q is None else q

        op = app.operations[0]
        status, body, _ = run_op(op, make_ctx(path="/items"))
        assert status == 200
        assert body == b"missing"

    def test_optional_query_in_doc_marked_not_required(self):
        app = HttpApp()

        @app.get("/items")
        def items(q: str | None = None) -> str:
            return q or ""

        doc = app.openapi_doc.to_dict()
        params = doc["paths"]["/items"]["get"]["parameters"]
        assert params[0]["name"] == "q"
        # OpenAPI omits ``required`` when false.
        assert params[0].get("required") is not True

    def test_optional_int_coerced_when_present(self):
        app = HttpApp()

        @app.get("/items")
        def items(limit: int | None = None) -> int:
            return limit if limit is not None else -1

        op = app.operations[0]
        status, body, _ = run_op(op, make_ctx(path="/items", query=b"limit=5"))
        assert status == 200
        assert msgspec.json.decode(body) == 5


class TestOptionalHeader:
    def test_optional_header_returns_none_when_absent(self):
        app = HttpApp()

        @app.get("/me")
        def me(x_id: Annotated[str | None, FromHeader("X-Id")] = None) -> str:
            return "anon" if x_id is None else x_id

        op = app.operations[0]
        status, body, _ = run_op(op, make_ctx(path="/me"))
        assert status == 200
        assert body == b"anon"

    def test_optional_header_in_doc(self):
        app = HttpApp()

        @app.get("/me")
        def me(x_id: Annotated[str | None, FromHeader("X-Id")] = None) -> str:
            return x_id or ""

        params = app.openapi_doc.to_dict()["paths"]["/me"]["get"]["parameters"]
        assert params[0]["name"] == "X-Id"
        assert params[0].get("required") is not True


# --- operationId derivation ----------------------------------------------


class TestOperationId:
    def test_operation_id_from_function_name(self):
        app = HttpApp()

        @app.get("/books/{book_id}")
        def get_book(book_id: str) -> str:
            return book_id

        doc = app.openapi_doc.to_dict()
        assert doc["paths"]["/books/{book_id}"]["get"]["operationId"] == "get_book"

    def test_lambda_falls_back_to_path_id(self):
        app = HttpApp()
        app.get("/foo")(lambda: "x")

        doc = app.openapi_doc.to_dict()
        assert doc["paths"]["/foo"]["get"]["operationId"] == "get_foo"


# --- Registration-time validation ----------------------------------------


class TestRegistrationValidation:
    def test_from_path_with_unknown_var_errors(self):
        app = HttpApp()

        with pytest.raises(ValueError, match="no such variable"):

            @app.get("/items")
            def items(item_id: Annotated[str, FromPath("item_id")]) -> str:
                return item_id

    def test_path_template_var_without_matching_param_errors(self):
        app = HttpApp()

        with pytest.raises(ValueError, match="no parameter resolves"):

            @app.get("/items/{item_id}")
            def items() -> str:
                return "x"

    def test_explicit_from_query_for_path_var_errors_with_unbound(self):
        # If the user accidentally uses FromQuery for a path var, the
        # path var goes unbound — caught at registration time.
        app = HttpApp()

        with pytest.raises(ValueError, match="no parameter resolves"):

            @app.get("/items/{item_id}")
            def items(item_id: Annotated[str, FromQuery("item_id")]) -> str:
                return item_id
