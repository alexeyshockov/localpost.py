# attrs / cattrs support in `localpost.openapi`

## Context

`localpost.openapi` already has a pluggable type-adapter seam
(`localpost/openapi/adapters/__init__.py`): a `TypeAdapter` protocol with
`claims` / `is_body_type` / `schema` / `components` / `decode` / `encode`,
dispatched by an `AdapterRegistry` whose last entry is a catch-all. Today
two adapters ship: `MsgspecAdapter` (catch-all, in `_msgspec.py`) and
`PydanticAdapter` (lazy-imported, in `_pydantic.py`). The README's
adapters docstring even names attrs as a future plug-in example.

Goal: make `attrs.define`'d classes first-class — bodies decoded,
responses encoded, OpenAPI schemas generated — by adding a third adapter,
without touching anything outside `localpost/openapi/adapters/`
(plus the protocol shim in step 1, the registry wiring, and packaging).

`attrs` is already installed transitively in the dev env (26.1.0);
`cattrs` is not.

## Decisions

1. **One adapter, attrs + cattrs together.** Require both at runtime when
   the adapter is active. attrs alone forces us to reinvent
   structuring/unstructuring; cattrs alone has no schema generator.
   Treat cattrs as the implementation detail of decode/encode and attrs
   as the introspection source for schemas.
2. **Position in `default_registry()`: `[pydantic?, attrs?, msgspec]`.**
   attrs goes before msgspec because msgspec's `is_body_type` would
   otherwise miss attrs classes (no `__dataclass_fields__`).
3. **Default cattrs converter**, overridable. Module-level
   `cattrs.preconf.json.make_converter()` for default; allow
   `AttrsAdapter(converter=...)` for users with custom hooks. Keep the
   constructor tiny — no DI, no decorators.
4. **Schema strategy: hand-rolled walker that re-enters the registry.**
   Walk `attrs.fields(cls)` and produce JSON Schema; for nested non-attrs
   types, ask the registry. This requires a small additive change to the
   `TypeAdapter` protocol (step 1 below). Other options were considered
   and rejected:
   - Convert attrs → dataclass at registration time and hand to msgspec.
     Fragile: validators, converters, `field(default=...)` semantics
     diverge from `dataclasses.field`.
   - Use `cattrs-extras` or similar for schema. Adds another moving part.
5. **Validators-as-constraints out of scope for the first cut.** Emit
   structural schema only (types, requiredness, `$ref`s). attrs
   validators don't have a uniform `__metadata__` shape; mapping
   `validators.in_` → `enum`, `matches_re` → `pattern`, etc., is a
   follow-up.
6. **Keep `attrs`/`cattrs` out of the base `openapi` extra.** Same posture
   as pydantic — users opt in.

## Work breakdown

### Step 1 — Extend `TypeAdapter` to allow registry recursion (additive)

Files: `localpost/openapi/adapters/__init__.py`, `_msgspec.py`,
`_pydantic.py`, `localpost/openapi/schemas.py`.

Why: the attrs adapter's `schema()` needs to recurse into nested types
(another attrs class, a msgspec.Struct, a pydantic model, a primitive)
without knowing which adapter owns each one. The registry already has
that dispatch.

Change shape (kept additive — no breakage to external `TypeAdapter`
implementers):

```python
# adapters/__init__.py
SchemaFor = Callable[[Any], dict[str, Any]]  # ref or inline

class TypeAdapter(Protocol):
    ...
    def schema(self, t: Any, /, *, ref_template: str,
               schema_for: SchemaFor | None = None) -> dict[str, Any]: ...
    def components(self, types: Sequence[Any], /, *, ref_template: str,
                   schema_for: SchemaFor | None = None
                   ) -> dict[str, dict[str, Any]]: ...
```

- Existing implementations (`MsgspecAdapter`, `PydanticAdapter`) accept
  the new kwarg and ignore it.
- `SchemaRegistry.schema_for` passes itself as the `schema_for` callback
  when delegating, so nested types coming back through the registry hit
  the right adapter.
- `SchemaRegistry.components()` likewise threads `schema_for` through.

Tradeoff: the protocol grows by one optional kwarg. Acceptable — the
README already calls these adapters "expected to be stateless and cheap",
and the kwarg is opt-in.

### Step 2 — `AttrsAdapter`

New file: `localpost/openapi/adapters/_attrs.py`.

Skeleton:

```python
from __future__ import annotations
from collections.abc import Sequence
from typing import Any, get_args, get_origin, Union, Literal
import attrs
import cattrs
from cattrs.errors import BaseValidationError
from cattrs.preconf.json import make_converter

_JSON = "application/json"
_DEFAULT_CONVERTER = make_converter()


class AttrsAdapter:
    name = "attrs"
    validation_errors: tuple[type[Exception], ...] = (BaseValidationError,)

    def __init__(self, converter: cattrs.Converter | None = None) -> None:
        self._converter = converter or _DEFAULT_CONVERTER

    def claims(self, t: Any, /) -> bool:
        return isinstance(t, type) and attrs.has(t)

    def is_body_type(self, t: Any, /) -> bool:
        return self.claims(t)

    def schema(self, t, /, *, ref_template, schema_for=None):
        if self.claims(t):
            return {"$ref": ref_template.format(name=t.__name__)}
        # Non-attrs nested type — defer to the registry if we have it,
        # otherwise emit empty (permissive) schema.
        return schema_for(t) if schema_for else {}

    def components(self, types, /, *, ref_template, schema_for=None):
        out: dict[str, dict[str, Any]] = {}
        for t in types:
            out[t.__name__] = self._build_object_schema(
                t, ref_template=ref_template, schema_for=schema_for
            )
        return out

    def decode(self, body, t, /, *, content_type):
        if not body:
            raise ValueError("empty request body")
        return self._converter.loads(body, t)

    def encode(self, value, /):
        return self._converter.dumps(value).encode("utf-8"), _JSON

    def _build_object_schema(self, t, *, ref_template, schema_for):
        properties: dict[str, dict[str, Any]] = {}
        required: list[str] = []
        for f in attrs.fields(t):
            field_schema = self._field_schema(
                f.type, ref_template=ref_template, schema_for=schema_for
            )
            properties[f.name] = field_schema
            if f.default is attrs.NOTHING:
                required.append(f.name)
        out: dict[str, Any] = {
            "type": "object",
            "title": t.__name__,
            "properties": properties,
        }
        if required:
            out["required"] = required
        return out

    def _field_schema(self, t, *, ref_template, schema_for):
        # Resolve string annotations once at registration time;
        # attrs.resolve_types(cls) called from _build_object_schema.
        ...
        # Dispatch:
        # - None / type(None) -> {"type": "null"}
        # - Union/Optional -> oneOf, dropping None for required check
        # - list[T], tuple[T, ...] -> {"type": "array", "items": ...}
        # - dict[str, V] -> {"type": "object", "additionalProperties": ...}
        # - Literal[...] -> {"enum": [...]}
        # - attrs class -> $ref via self.schema(...)
        # - everything else -> schema_for(t) if available else {}
```

Key points:

- Call `attrs.resolve_types(t)` once per class in `components()` so
  `f.type` is a real type, not a string. Cache the resolved set on the
  adapter instance (`set[type]`) to avoid repeated work.
- Validation error mapping: cattrs raises `ClassValidationError` which is
  a `BaseValidationError`; using the base catches the whole family. This
  flows through the existing `FromBody` catch (`resolvers.py:374`) which
  produces a `BadRequest`.
- Nested attrs types referenced by a field: emit `$ref` from
  `_field_schema`, then **also** notify the registry so it gets added to
  components. The cleanest way is to call `schema_for(nested_attrs_t)` —
  the registry both records the type and returns the `$ref`. So
  `_field_schema` can route every non-trivial branch through `schema_for`
  for a uniform path.

### Step 3 — Wire into `default_registry()`

`localpost/openapi/adapters/__init__.py`:

```python
@functools.cache
def default_registry() -> AdapterRegistry:
    from localpost.openapi.adapters._msgspec import MsgspecAdapter
    adapters: list[TypeAdapter] = []
    try:
        from localpost.openapi.adapters._pydantic import PydanticAdapter
        adapters.append(PydanticAdapter())
    except ImportError:
        pass
    try:
        from localpost.openapi.adapters._attrs import AttrsAdapter
        adapters.append(AttrsAdapter())
    except ImportError:
        pass
    adapters.append(MsgspecAdapter())
    return AdapterRegistry(adapters)
```

Order matters: pydantic first (most specific — `BaseModel` subclass
check), then attrs (`attrs.has`), then msgspec catch-all.

### Step 4 — Packaging

`pyproject.toml`:

- New optional extra (does NOT touch the base `openapi` extra):
  ```toml
  openapi-attrs = [
      "attrs >=23",
      "cattrs >=24",
  ]
  ```
- `dev-openapi` group adds `attrs` and `cattrs` so our own examples and
  tests exercise the path:
  ```toml
  dev-openapi = [
      "pydantic ~=2.7",
      "attrs ~=24.2",
      "cattrs ~=24.1",
  ]
  ```

### Step 5 — Tests

- `tests/openapi/schemas.py`: mirror the pydantic block (lines 114-) with
  two tests:
  - `test_attrs_class_routes_to_attrs` — registry dispatches to
    `name == "attrs"`.
  - `test_attrs_schema_registered_in_components` — components include
    the attrs class with expected `properties` / `required`.
  - Bonus: `test_attrs_with_nested_msgspec_struct` — exercise the
    `schema_for` recursion path (attrs class containing a
    `msgspec.Struct` field, components must contain both).
- `tests/openapi/app.py`: end-to-end `test_attrs_body_is_parsed_and_serialized`
  modeled on the pydantic test at line 405. Validation error should
  produce a 400.
- All gated with `pytest.importorskip("attrs")` and
  `pytest.importorskip("cattrs")`.

### Step 6 — Example

Add `examples/openapi/app_attrs.py` (small variant of `app.py`) using
`@attrs.define` for `Book`. Keeps the README quickstart untouched.

### Step 7 — Docs

- `localpost/openapi/README.md`:
  - Argument-resolvers table (line 102): widen `FromBody` row to
    "msgspec.Struct / dataclass / pydantic model / **attrs class**".
  - Install section (line 22): note `pip install attrs cattrs` for attrs
    handler support, alongside the existing pydantic note.
- `localpost/openapi/adapters/__init__.py` module docstring already names
  attrs as the example custom adapter — update the snippet to reference
  the now-built-in `AttrsAdapter` and clarify users only need a custom
  one for *other* libraries.

## Risks / open questions

1. **Forward references in field types.** `attrs.fields(cls).type` may be
   a string if the class uses `from __future__ import annotations`.
   Mitigation: `attrs.resolve_types(cls)` at the start of
   `_build_object_schema`. Wraps cleanly; `attrs` ships this helper.
2. **cattrs version.** Major releases have shuffled `preconf.json` and
   exception class names. Pin `cattrs >=24` and verify on 24.1 + latest.
3. **Generic attrs classes (`@attrs.define` with `Generic[T]`).** Punt
   for the first cut — emit the un-parameterised schema. Document.
4. **Free-threading.** `_DEFAULT_CONVERTER` is a module-level shared
   object; cattrs converters are not documented as thread-safe for
   register-on-first-use, but our default has no late registration.
   Should be safe; flag for verification.
5. **Schema-walker scope creep.** The walker WILL grow (Annotated,
   nested generics, recursive types). Keep it in `_attrs.py` and don't
   let it leak into `schemas.py`. If a future adapter (e.g. protobuf)
   needs the same primitives, factor then — not now.

## Out of scope (explicit)

- attrs without cattrs.
- attrs validators → JSON Schema constraints.
- Custom cattrs hook registration via `@app`-level decorators.
- `attr.s` (legacy API) — only the modern `attrs.define` / `@attrs.frozen`
  are tested. Should work via `attrs.has` but not promised.
