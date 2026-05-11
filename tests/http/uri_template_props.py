"""Property-based tests for ``URITemplate`` (RFC 6570 Level 1)."""

from __future__ import annotations

import re

from hypothesis import assume, given
from hypothesis import strategies as st

from localpost.http import URITemplate

# --- Strategies ---------------------------------------------------------------

# Path segment chars: visible ASCII minus '/' and '{' / '}' (which would
# accidentally re-introduce template syntax).
_segment_chars = st.characters(
    min_codepoint=33,
    max_codepoint=126,
    blacklist_characters="/{}",
)

_literal_segment = st.text(_segment_chars, min_size=1, max_size=8)
_variable_name = st.from_regex(r"\A[a-zA-Z_][a-zA-Z0-9_]{0,7}\Z")


@st.composite
def _template_with_values(draw) -> tuple[str, dict[str, str], str]:
    """Build (template, values, concrete_uri) where values substitute cleanly."""
    n_segments = draw(st.integers(min_value=1, max_value=5))
    template_parts: list[str] = []
    uri_parts: list[str] = []
    values: dict[str, str] = {}
    used_names: set[str] = set()

    for _ in range(n_segments):
        kind = draw(st.sampled_from(["literal", "variable"]))
        if kind == "literal":
            seg = draw(_literal_segment)
            template_parts.append(seg)
            uri_parts.append(seg)
        else:
            name = draw(_variable_name.filter(lambda n: n not in used_names))
            used_names.add(name)
            value = draw(st.text(_segment_chars, min_size=1, max_size=8))
            template_parts.append("{" + name + "}")
            uri_parts.append(value)
            values[name] = value

    template = "/" + "/".join(template_parts)
    uri = "/" + "/".join(uri_parts)
    return template, values, uri


# --- Properties ---------------------------------------------------------------


class TestURITemplateProperties:
    @given(payload=_template_with_values())
    def test_round_trip(self, payload):
        """match(uri_built_from_values) should yield the same values back."""
        template, values, uri = payload
        t = URITemplate.parse(template)
        result = t.match(uri)
        assert result == values, f"template={template!r} uri={uri!r} expected={values!r} got={result!r}"

    @given(payload=_template_with_values())
    def test_variable_names_in_declaration_order(self, payload):
        template, values, _ = payload
        t = URITemplate.parse(template)
        expected_order = tuple(re.findall(r"\{([^}]+)\}", template))
        assert t.variable_names == expected_order
        # And every name we substituted is present in variable_names.
        assert set(values).issubset(set(t.variable_names))

    @given(template=_literal_segment.map(lambda s: f"/{s}"))
    def test_literal_template_matches_self_only(self, template):
        t = URITemplate.parse(template)
        assert t.match(template) == {}
        assert t.match(template + "/extra") is None
        assert t.match(template[:-1]) is None  # one char short

    @given(payload=_template_with_values(), extra=_literal_segment)
    def test_extra_segments_do_not_match(self, payload, extra):
        """A path that has more segments than the template should not match."""
        template, _, uri = payload
        t = URITemplate.parse(template)
        assert t.match(uri + "/" + extra) is None

    @given(payload=_template_with_values())
    def test_variable_does_not_span_slash(self, payload):
        """A value with a '/' in it should never match a single-var slot."""
        template, values, _ = payload
        # Pick the test only when there is at least one variable.
        assume(values)
        # Build a uri where one var value contains '/' — should fail to match.
        bad_values = {**values, next(iter(values)): "x/y"}
        bad_uri_parts = []
        for part in re.split(r"(\{[^}]+\})", template):
            if part.startswith("{") and part.endswith("}"):
                name = part[1:-1]
                bad_uri_parts.append(bad_values[name])
            else:
                bad_uri_parts.append(part)
        bad_uri = "".join(bad_uri_parts)
        t = URITemplate.parse(template)
        assert t.match(bad_uri) is None
