"""Tests for the resolver / Operation polish: Optional handling,
operationId derivation, registration-time validation."""

from __future__ import annotations

from typing import Annotated

import msgspec
import pytest

from localpost.openapi import FromHeader, FromPath, FromQuery, HttpApp, NotFound
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


class TestOptionalReturnIsNotFound:
    """``T | None`` return → implicit 404 NotFound when ``None`` is returned."""

    def test_none_return_yields_404(self):
        app = HttpApp()

        @app.get("/items/{item_id}")
        def get_item(item_id: str) -> str | None:
            return None if item_id == "missing" else item_id

        op = app.operations[0]
        status, body, _ = run_op(op, make_ctx(path="/items/missing", path_args={"item_id": "missing"}))
        assert status == 404
        assert body == b""

    def test_value_return_still_yields_200(self):
        app = HttpApp()

        @app.get("/items/{item_id}")
        def get_item(item_id: str) -> str | None:
            return item_id

        op = app.operations[0]
        status, body, _ = run_op(op, make_ctx(path="/items/x", path_args={"item_id": "x"}))
        assert status == 200
        assert body == b"x"

    def test_doc_includes_both_200_and_404(self):
        app = HttpApp()

        @app.get("/items/{item_id}")
        def get_item(item_id: str) -> str | None:
            return item_id

        responses = app.openapi_doc.to_dict()["paths"]["/items/{item_id}"]["get"]["responses"]
        assert set(responses) == {"200", "404"}
        assert responses["404"] == {"description": "Not Found"}

    def test_explicit_not_found_takes_precedence(self):
        # If the user already declared NotFound[X], the implicit None→404
        # shouldn't overwrite it (and shouldn't add a body-less duplicate).
        app = HttpApp()

        @app.get("/items/{item_id}")
        def get_item(item_id: str) -> str | NotFound[str] | None:
            return None if item_id == "missing" else item_id

        responses = app.openapi_doc.to_dict()["paths"]["/items/{item_id}"]["get"]["responses"]
        # Only one 404 — the explicit NotFound[str] wins (has a JSON body schema).
        assert "404" in responses
        assert "content" in responses["404"]
        assert responses["404"]["content"]["application/json"]["schema"] == {"type": "string"}

    def test_bare_none_return_is_not_404(self):
        # ``-> None`` (no Union) should still mean "200 / empty success",
        # not 404.
        app = HttpApp()

        @app.get("/ping")
        def ping() -> None:
            return None

        op = app.operations[0]
        status, _, _ = run_op(op, make_ctx(path="/ping"))
        assert status == 200


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
