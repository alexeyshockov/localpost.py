"""pydantic :class:`TypeAdapter`. Imported lazily by ``default_registry``;
import succeeds only when pydantic is installed.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ValidationError

__all__ = ["PydanticAdapter"]


_JSON_CONTENT_TYPE = "application/json"


class PydanticAdapter:
    name = "pydantic"
    validation_errors: tuple[type[Exception], ...] = (ValidationError,)

    def claims(self, t: Any, /) -> bool:
        return isinstance(t, type) and issubclass(t, BaseModel)

    def is_body_type(self, t: Any, /) -> bool:
        return self.claims(t)

    def schema(self, t: Any, /, *, ref_template: str) -> dict[str, Any]:
        # Body emitted in components(); inline ref keeps the schema fragment
        # consistent with msgspec's $ref-for-named-types behaviour.
        return {"$ref": ref_template.format(name=t.__name__)}

    def components(self, types: Sequence[Any], /, *, ref_template: str) -> dict[str, dict[str, Any]]:
        # Pydantic's placeholder is ``{model}``, not ``{name}``.
        pydantic_template = ref_template.replace("{name}", "{model}")
        out: dict[str, dict[str, Any]] = {}
        for t in types:
            raw: dict[str, Any] = t.model_json_schema(ref_template=pydantic_template)
            raw.pop("$defs", None)
            out[t.__name__] = raw
        return out

    def decode(self, body: bytes, t: Any, /, *, content_type: str) -> object:
        if not body:
            raise ValueError("empty request body")
        return t.model_validate_json(body)

    def encode(self, value: object, /) -> tuple[bytes, str]:
        assert isinstance(value, BaseModel)
        return value.model_dump_json().encode("utf-8"), _JSON_CONTENT_TYPE
