"""Tests for the adapter-driven JSON Schema registry."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

import msgspec
import pytest

from localpost.openapi.adapters import AdapterRegistry, default_registry
from localpost.openapi.adapters._msgspec import MsgspecAdapter
from localpost.openapi.schemas import REF_TEMPLATE, SchemaRegistry


@dataclass
class Book:
    id: str
    title: str


class Author(msgspec.Struct):
    name: str
    age: int


class Status(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


class TestPrimitives:
    def test_int(self):
        registry = SchemaRegistry()
        assert registry.schema_for(int) == {"type": "integer"}

    def test_str(self):
        registry = SchemaRegistry()
        assert registry.schema_for(str) == {"type": "string"}

    def test_bool(self):
        registry = SchemaRegistry()
        assert registry.schema_for(bool) == {"type": "boolean"}

    def test_none(self):
        registry = SchemaRegistry()
        assert registry.schema_for(None) == {"type": "null"}
        assert registry.schema_for(type(None)) == {"type": "null"}


class TestNamedTypes:
    def test_dataclass_returns_ref(self):
        registry = SchemaRegistry()
        schema = registry.schema_for(Book)
        assert schema == {"$ref": REF_TEMPLATE.format(name="Book")}

    def test_struct_returns_ref(self):
        registry = SchemaRegistry()
        schema = registry.schema_for(Author)
        assert schema == {"$ref": REF_TEMPLATE.format(name="Author")}

    def test_components_includes_referenced_types(self):
        registry = SchemaRegistry()
        registry.schema_for(Book)
        registry.schema_for(Author)
        components = registry.components()

        assert "Book" in components
        assert "Author" in components
        assert components["Book"]["type"] == "object"
        assert "title" in components["Book"]["properties"]


class TestEnums:
    def test_str_enum_emits_values(self):
        registry = SchemaRegistry()
        registry.schema_for(Status)
        components = registry.components()
        assert "Status" in components
        assert set(components["Status"]["enum"]) == {"open", "closed"}


class TestLiteral:
    def test_literal_inlines_enum(self):
        registry = SchemaRegistry()
        schema = registry.schema_for(Literal["a", "b"])
        # Literal types are inlined by msgspec, not registered as components.
        assert "enum" in schema or "const" in schema or "$ref" in schema


class TestComponentCacheInvalidation:
    def test_components_recomputed_after_new_registration(self):
        registry = SchemaRegistry()
        registry.schema_for(Book)
        assert "Book" in registry.components()

        registry.schema_for(Author)
        components = registry.components()
        assert "Book" in components
        assert "Author" in components


class TestAdapterDispatch:
    def test_dataclass_routes_to_msgspec(self):
        registry = default_registry()
        assert registry.for_type(Book).name == "msgspec"

    def test_msgspec_struct_routes_to_msgspec(self):
        registry = default_registry()
        assert registry.for_type(Author).name == "msgspec"

    def test_pydantic_model_routes_to_pydantic(self):
        BaseModel = pytest.importorskip("pydantic").BaseModel

        class Pet(BaseModel):
            name: str

        registry = default_registry()
        assert registry.for_type(Pet).name == "pydantic"

    def test_pydantic_schema_registered_in_components(self):
        BaseModel = pytest.importorskip("pydantic").BaseModel

        class Pet(BaseModel):
            name: str
            age: int

        registry = SchemaRegistry()
        schema = registry.schema_for(Pet)
        assert schema == {"$ref": REF_TEMPLATE.format(name="Pet")}

        components = registry.components()
        assert "Pet" in components
        assert "name" in components["Pet"]["properties"]


class TestCustomAdapter:
    """Plug a fake adapter ahead of msgspec to confirm dispatch is extensible."""

    class _Marker:
        """Sentinel type the fake adapter claims."""

    class _FakeAdapter:
        name = "fake"
        validation_errors: tuple[type[Exception], ...] = ()

        def __init__(self) -> None:
            self.schema_calls: list[Any] = []
            self.components_calls: list[Sequence[Any]] = []

        def claims(self, t: Any, /) -> bool:
            return t is TestCustomAdapter._Marker

        def is_body_type(self, t: Any, /) -> bool:
            return self.claims(t)

        def schema(self, t: Any, /, *, ref_template: str) -> dict[str, Any]:
            self.schema_calls.append(t)
            return {"$ref": ref_template.format(name="Marker")}

        def components(
            self, types: Sequence[Any], /, *, ref_template: str
        ) -> dict[str, dict[str, Any]]:
            self.components_calls.append(types)
            return {"Marker": {"type": "object", "x-fake": True}}

        def decode(self, body: bytes, t: Any, /, *, content_type: str) -> object:
            raise NotImplementedError

        def encode(self, value: object, /) -> tuple[bytes, str]:
            return b'"fake"', "application/json"

    def test_custom_adapter_wins_over_catch_all(self):
        fake = self._FakeAdapter()
        registry = AdapterRegistry([fake, MsgspecAdapter()])

        schema_registry = SchemaRegistry(registry)
        schema = schema_registry.schema_for(self._Marker)
        assert schema == {"$ref": REF_TEMPLATE.format(name="Marker")}
        assert fake.schema_calls == [self._Marker]

        components = schema_registry.components()
        assert components["Marker"] == {"type": "object", "x-fake": True}
        # And the catch-all is still active for non-claimed types.
        schema_registry.schema_for(Book)
        assert "Book" in schema_registry.components()
