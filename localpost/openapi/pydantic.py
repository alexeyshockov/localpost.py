"""Pydantic interop for :mod:`localpost.openapi`.

Pydantic is **not** a runtime dependency of localpost. To use this module,
install pydantic yourself::

    pip install pydantic

Once available, :class:`localpost.openapi.HttpApp` recognises pydantic
models *automatically*: a parameter annotated as ``MyModel`` is parsed
from the request body via :meth:`BaseModel.model_validate_json`, and a
return value of type ``MyModel`` is serialised via
:meth:`BaseModel.model_dump_json`. The OpenAPI schema for the model is
sourced from :meth:`BaseModel.model_json_schema`.

This module re-exports the parsing / serialising helpers for users who
want to call them explicitly (or build custom resolvers / response
converters on top).
"""

from __future__ import annotations

from typing import Any

from localpost.openapi.schemas import _PydanticBaseModel, is_pydantic_model

__all__ = ["is_pydantic_model", "decode_body", "encode_body"]


def decode_body(model: type[Any], body: bytes) -> Any:
    """Validate ``body`` (raw JSON bytes) into an instance of ``model``.

    ``model`` must be a :class:`pydantic.BaseModel` subclass.

    Raises :exc:`pydantic.ValidationError` on invalid input.
    """
    if _PydanticBaseModel is None:
        raise RuntimeError("pydantic is not installed")
    if not is_pydantic_model(model):
        raise TypeError(f"Expected a pydantic BaseModel subclass, got {model!r}")
    return getattr(model, "model_validate_json")(body)  # noqa: B009


def encode_body(value: Any) -> bytes:
    """Encode a pydantic model instance to JSON bytes via ``model_dump_json``."""
    if _PydanticBaseModel is None:
        raise RuntimeError("pydantic is not installed")
    if not is_pydantic_model(type(value)):
        raise TypeError(f"Expected a pydantic BaseModel instance, got {type(value)!r}")
    return getattr(value, "model_dump_json")().encode("utf-8")  # noqa: B009
