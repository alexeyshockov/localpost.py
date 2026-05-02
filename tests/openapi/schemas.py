"""Tests for the msgspec-backed JSON Schema registry."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

import msgspec
import pytest

from localpost.openapi.schemas import REF_TEMPLATE, SchemaRegistry, is_pydantic_model


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


class TestPydanticDetection:
    def test_dataclass_is_not_pydantic(self):
        assert is_pydantic_model(Book) is False

    def test_msgspec_struct_is_not_pydantic(self):
        assert is_pydantic_model(Author) is False

    def test_pydantic_model_detected(self):
        BaseModel = pytest.importorskip("pydantic").BaseModel

        class Pet(BaseModel):
            name: str

        assert is_pydantic_model(Pet) is True

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
