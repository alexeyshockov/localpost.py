"""Tests for ``localpost.openapi.HttpApp`` registration + dispatch.

Operation handlers are exercised in isolation: we hand each ``Operation``
a fake :class:`HTTPReqCtx`-like object, run it, and inspect the captured
``Response`` + body bytes. The full HTTP server path is covered by the
integration tests.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from http import HTTPMethod
from typing import Annotated, Any

import msgspec
import pytest

from localpost.http import RouteMatch, URITemplate
from localpost.http._types import Request, Response
from localpost.openapi import (
    BadRequest,
    Created,
    FromHeader,
    FromPath,
    HttpApp,
    NoContent,
    NotFound,
)
from localpost.openapi.operation import Operation

# --- Fakes ---------------------------------------------------------------


@dataclass
class FakeCtx:
    """Minimal stand-in for :class:`HTTPReqCtx` for unit tests."""

    request: Request
    body: bytes = b""
    attrs: dict[Any, Any] = field(default_factory=dict)
    completed: tuple[Response, bytes] | None = None

    def complete(self, response: Response, body: bytes = b"") -> None:
        self.completed = (response, body)


def make_ctx(
    method: str = "GET",
    path: str = "/",
    query: bytes = b"",
    headers: list[tuple[bytes, bytes]] | None = None,
    body: bytes = b"",
    path_args: Mapping[str, str] | None = None,
    template: URITemplate | None = None,
) -> FakeCtx:
    request = Request(
        method=method.encode(),
        target=path.encode(),
        path=path.encode(),
        query_string=query,
        headers=headers or [],
        http_version=b"1.1",
    )
    ctx = FakeCtx(request=request, body=body)
    if path_args is not None:
        ctx.attrs[RouteMatch] = RouteMatch(
            method=HTTPMethod(method),
            matched_template=template or URITemplate.parse(path),
            path_args=dict(path_args),
        )
    return ctx


def run_op(op: Operation, ctx: FakeCtx) -> tuple[int, bytes, dict[str, str]]:
    op._run(ctx)
    assert ctx.completed is not None, "handler did not complete the response"
    response, body = ctx.completed
    headers = {k.decode(): v.decode("iso-8859-1") for k, v in response.headers}
    return response.status_code, body, headers


# --- Sample types --------------------------------------------------------


@dataclass
class Book:
    id: str
    title: str


# Pydantic Pet lives at module scope so PEP 563 string annotations resolve via
# ``get_type_hints`` (closure cells aren't created for annotation-only refs).
try:
    from pydantic import BaseModel

    class Pet(BaseModel):
        name: str
        age: int

except ImportError:
    Pet = None  # type: ignore[assignment,misc]


# --- Tests ---------------------------------------------------------------


class TestRegistration:
    def test_get_decorator_records_operation(self):
        app = HttpApp()

        @app.get("/foo")
        def foo() -> str:
            return "ok"

        assert len(app.operations) == 1
        op = app.operations[0]
        assert op.method is HTTPMethod.GET
        assert op.path == "/foo"

    def test_multiple_operations_on_same_path(self):
        app = HttpApp()

        @app.get("/foo")
        def get_foo() -> str:
            return "g"

        @app.post("/foo")
        def post_foo() -> str:
            return "p"

        ops = app.operations
        assert {o.method for o in ops} == {HTTPMethod.GET, HTTPMethod.POST}

    def test_path_var_resolved_implicitly(self):
        app = HttpApp()

        @app.get("/items/{item_id}")
        def get_item(item_id: str) -> str:
            return item_id

        op = app.operations[0]
        ctx = make_ctx(path="/items/abc", path_args={"item_id": "abc"})
        status, body, _ = run_op(op, ctx)
        assert status == 200
        assert body == b"abc"

    def test_query_param_implicit_when_not_in_path(self):
        app = HttpApp()

        @app.get("/items/{item_id}")
        def get_item(item_id: str, page: int = 1) -> str:
            return f"{item_id}/{page}"

        op = app.operations[0]
        ctx = make_ctx(path="/items/x", query=b"page=3", path_args={"item_id": "x"})
        status, body, _ = run_op(op, ctx)
        assert status == 200
        assert body == b"x/3"


class TestReturnTypeInference:
    def test_str_return_emits_text_plain(self):
        app = HttpApp()

        @app.get("/hi")
        def hi() -> str:
            return "hello"

        op = app.operations[0]
        ctx = make_ctx(path="/hi")
        status, body, headers = run_op(op, ctx)
        assert status == 200
        assert body == b"hello"
        assert headers["content-type"].startswith("text/plain")

    def test_dataclass_return_emits_json(self):
        app = HttpApp()

        @app.get("/b")
        def get() -> Book:
            return Book(id="1", title="t")

        op = app.operations[0]
        status, body, headers = run_op(op, make_ctx(path="/b"))
        assert status == 200
        assert msgspec.json.decode(body) == {"id": "1", "title": "t"}
        assert headers["content-type"] == "application/json"

    def test_op_result_subclass_uses_its_status(self):
        app = HttpApp()

        @app.get("/b/{id}")
        def get(id: str) -> Book | NotFound[str]:  # noqa: A002
            return NotFound(f"missing: {id}")

        op = app.operations[0]
        ctx = make_ctx(path="/b/x", path_args={"id": "x"})
        status, body, _ = run_op(op, ctx)
        assert status == 404
        assert body == b"missing: x"

    def test_no_content_emits_empty_body(self):
        app = HttpApp()

        @app.delete("/b/{id}")
        def delete(id: str) -> NoContent:  # noqa: A002
            return NoContent()

        op = app.operations[0]
        ctx = make_ctx(method="DELETE", path="/b/x", path_args={"id": "x"})
        status, body, _ = run_op(op, ctx)
        assert status == 204
        assert body == b""

    def test_created_with_custom_headers(self):
        app = HttpApp()

        @app.post("/b")
        def create() -> Created[Book]:
            return Created(Book(id="1", title="t"), headers={"Location": "/b/1"})

        op = app.operations[0]
        status, body, headers = run_op(op, make_ctx(method="POST", path="/b"))
        assert status == 201
        assert headers["Location"] == "/b/1"
        assert msgspec.json.decode(body) == {"id": "1", "title": "t"}


class TestFromBody:
    def test_dataclass_body_is_parsed(self):
        app = HttpApp()

        @app.post("/b")
        def create(book: Book) -> Book:
            return book

        op = app.operations[0]
        ctx = make_ctx(method="POST", path="/b", body=b'{"id":"1","title":"X"}')
        status, body, _ = run_op(op, ctx)
        assert status == 200
        assert msgspec.json.decode(body) == {"id": "1", "title": "X"}

    def test_invalid_body_returns_bad_request(self):
        app = HttpApp()

        @app.post("/b")
        def create(book: Book) -> Book:
            return book

        op = app.operations[0]
        ctx = make_ctx(method="POST", path="/b", body=b"not json")
        status, body, _ = run_op(op, ctx)
        assert status == 400
        assert b"Invalid request body" in body


class TestFromHeader:
    def test_header_resolved_with_explicit_annotation(self):
        app = HttpApp()

        @app.get("/me")
        def me(user_id: Annotated[str, FromHeader("X-User-Id")]) -> str:
            return user_id

        op = app.operations[0]
        ctx = make_ctx(path="/me", headers=[(b"x-user-id", b"alice")])
        status, body, _ = run_op(op, ctx)
        assert status == 200
        assert body == b"alice"

    def test_missing_required_header_returns_bad_request(self):
        app = HttpApp()

        @app.get("/me")
        def me(user_id: Annotated[str, FromHeader("X-User-Id")]) -> str:
            return user_id

        op = app.operations[0]
        ctx = make_ctx(path="/me")
        status, body, _ = run_op(op, ctx)
        assert status == 400
        assert b"Missing required header" in body

    def test_header_default_used_when_absent(self):
        app = HttpApp()

        @app.get("/me")
        def me(user_id: Annotated[str, FromHeader("X-User-Id")] = "anon") -> str:
            return user_id

        op = app.operations[0]
        ctx = make_ctx(path="/me")
        status, body, _ = run_op(op, ctx)
        assert status == 200
        assert body == b"anon"


class TestPathTypeCoercion:
    def test_int_path_arg_coerced(self):
        app = HttpApp()

        @app.get("/items/{item_id}")
        def get(item_id: int) -> int:
            return item_id * 2

        op = app.operations[0]
        ctx = make_ctx(path="/items/21", path_args={"item_id": "21"})
        status, body, _ = run_op(op, ctx)
        assert status == 200
        assert msgspec.json.decode(body) == 42

    def test_invalid_int_path_arg_returns_bad_request(self):
        app = HttpApp()

        @app.get("/items/{item_id}")
        def get(item_id: int) -> int:
            return item_id

        op = app.operations[0]
        ctx = make_ctx(path="/items/x", path_args={"item_id": "x"})
        status, body, _ = run_op(op, ctx)
        assert status == 400
        assert b"Invalid path parameter" in body


class TestExplicitFromPath:
    def test_explicit_from_path_overrides_query_default(self):
        # Same param name as a path var → FromPath is implicit; this
        # checks the explicit form still wires up correctly.
        app = HttpApp()

        @app.get("/items/{item_id}")
        def get(item_id: Annotated[str, FromPath()]) -> str:
            return item_id

        op = app.operations[0]
        ctx = make_ctx(path="/items/abc", path_args={"item_id": "abc"})
        status, body, _ = run_op(op, ctx)
        assert status == 200
        assert body == b"abc"


class TestOpenAPIDocCaching:
    def test_doc_cached_until_new_op_registered(self):
        app = HttpApp()

        @app.get("/a")
        def a() -> str:
            return "a"

        doc1 = app.openapi_doc
        doc2 = app.openapi_doc
        assert doc1 is doc2

        @app.get("/b")
        def b() -> str:
            return "b"

        doc3 = app.openapi_doc
        assert doc3 is not doc1
        assert "/b" in doc3.paths


class TestOpenAPIDocStructure:
    def test_response_schemas_per_status(self):
        app = HttpApp()

        @app.get("/b/{id}")
        def get(id: str) -> Book | NotFound[str] | BadRequest[str]:  # noqa: A002
            return Book(id=id, title="x")

        doc = app.openapi_doc.to_dict()
        responses = doc["paths"]["/b/{id}"]["get"]["responses"]
        assert set(responses) == {"200", "400", "404"}
        assert responses["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/Book")
        assert responses["404"]["content"]["application/json"]["schema"] == {"type": "string"}

    def test_request_body_schema_emitted(self):
        app = HttpApp()

        @app.post("/b")
        def create(book: Book) -> Book:
            return book

        doc = app.openapi_doc.to_dict()
        request_body = doc["paths"]["/b"]["post"]["requestBody"]
        assert request_body["required"] is True
        schema = request_body["content"]["application/json"]["schema"]
        assert schema["$ref"].endswith("/Book")

    def test_components_populated(self):
        app = HttpApp()

        @app.get("/b/{id}")
        def get(id: str) -> Book:  # noqa: A002
            return Book(id=id, title="x")

        doc = app.openapi_doc.to_dict()
        assert "Book" in doc["components"]["schemas"]


class TestPydantic:
    def test_pydantic_body_is_parsed_and_serialized(self):
        pytest.importorskip("pydantic")

        app = HttpApp()

        @app.post("/pets")
        def create(pet: Pet) -> Pet:
            return pet

        op = app.operations[0]
        ctx = make_ctx(method="POST", path="/pets", body=b'{"name":"Rex","age":3}')
        status, body, headers = run_op(op, ctx)
        assert status == 200, body
        assert msgspec.json.decode(body) == {"name": "Rex", "age": 3}
        assert headers["content-type"] == "application/json"
