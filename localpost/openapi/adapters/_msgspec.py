"""msgspec-based :class:`TypeAdapter` — the catch-all in the registry.

Understands :class:`msgspec.Struct`, dataclasses, ``TypedDict``,
``NamedTuple``, primitives, collections, ``Annotated[T, msgspec.Meta(...)]``,
``Literal``, ``Enum``, ``Union``, ``UUID``, ``datetime`` — i.e. everything
:func:`msgspec.json.schema_components` and :func:`msgspec.json.decode`
already handle.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import msgspec

__all__ = ["MsgspecAdapter"]


_JSON_CONTENT_TYPE = "application/json"


class MsgspecAdapter:
    name = "msgspec"
    validation_errors: tuple[type[Exception], ...] = (msgspec.ValidationError,)

    def claims(self, t: Any, /) -> bool:
        # Catch-all — placed last in the registry so more specific adapters
        # win first. Returning True unconditionally lets us also handle
        # primitives, generics, unions, and the long tail (UUID, datetime).
        return True

    def is_body_type(self, t: Any, /) -> bool:
        if not isinstance(t, type):
            return False
        if issubclass(t, msgspec.Struct):
            return True
        if hasattr(t, "__dataclass_fields__"):
            return True
        # NamedTuple: subclass of tuple with class-level field metadata.
        return hasattr(t, "__annotations__") and hasattr(t, "_fields")

    def schema(self, t: Any, /, *, ref_template: str) -> dict[str, Any]:
        try:
            (schema,), _ = msgspec.json.schema_components([t], ref_template=ref_template)
        except (TypeError, RuntimeError):
            # Fall back to a permissive schema for types msgspec can't introspect.
            return {}
        return schema

    def components(self, types: Sequence[Any], /, *, ref_template: str) -> dict[str, dict[str, Any]]:
        if not types:
            return {}
        _, components = msgspec.json.schema_components(list(types), ref_template=ref_template)
        return dict(components)

    def decode(self, body: bytes, t: Any, /, *, content_type: str) -> object:
        if not body:
            raise ValueError("empty request body")
        return msgspec.json.decode(body, type=t)

    def encode(self, value: object, /) -> tuple[bytes, str]:
        return msgspec.json.encode(value), _JSON_CONTENT_TYPE
