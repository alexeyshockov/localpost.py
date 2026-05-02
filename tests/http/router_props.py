"""Property-based tests for ``Router`` dispatch behavior.

Mirrors the spec in :func:`localpost.http.router.Router._match` and locks in:

- 200 / 404 / 405 outcome predicate — see ``router.py:241-287``
- longest-literal-prefix sort — see ``router.py:209-213``
- 405 ``Allow`` header is the sorted union of matching templates' methods
  — see ``router.py:281-285``

Companion to ``tests/http/router.py``, which holds example-based coverage
exercised through the full HTTP wire. Here we drive the dispatcher
in-process (no socket, no httpx) so the property suite stays fast.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from http import HTTPMethod
from typing import Any, cast

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from localpost.http import (
    BodyHandler,
    HTTPReqCtx,
    Request,
    Response,
    RouteMatch,
    Router,
    Routes,
    URITemplate,
)
from tests.http.uri_template_props import _literal_segment, _template_with_values

# --- Strategies -------------------------------------------------------------

_method = st.sampled_from(list(HTTPMethod))


@st.composite
def _route_set(draw) -> list[tuple[str, frozenset[HTTPMethod], str]]:
    """Draw a list of ``(template, methods, concrete_uri)`` triples.

    Templates are unique by string. ``concrete_uri`` is one valid URI built from
    the template — used to drive the matching path through the router.
    """
    n = draw(st.integers(min_value=1, max_value=5))
    triples: list[tuple[str, frozenset[HTTPMethod], str]] = []
    seen: set[str] = set()
    for _ in range(n):
        template, _values, concrete = draw(_template_with_values())
        if template in seen:
            continue
        seen.add(template)
        methods = frozenset(draw(st.sets(_method, min_size=1, max_size=5)))
        triples.append((template, methods, concrete))
    return triples


@st.composite
def _scenario(draw) -> tuple[list[tuple[str, frozenset[HTTPMethod], str]], HTTPMethod, str]:
    """Draw ``(routes, method, path)``.

    Half the time ``path`` is a concrete URI from one of the routes (likely 200
    or 405); the other half it is a random path (typically 404, occasionally
    matches a fully-variable template).
    """
    routes = draw(_route_set())
    if draw(st.booleans()):
        _tmpl, _methods, concrete = draw(st.sampled_from(routes))
        path = concrete
    else:
        n = draw(st.integers(min_value=1, max_value=4))
        path = "/" + "/".join(draw(_literal_segment) for _ in range(n))
    method = draw(_method)
    return routes, method, path


# --- Synthetic dispatch -----------------------------------------------------


@dataclass(eq=False, slots=True)
class _FakeCtx:
    """Minimal :class:`HTTPReqCtx` stand-in for in-process router dispatch.

    The router's ``as_handler()`` only touches ``ctx.request``, ``ctx.attrs``,
    and ``ctx.complete(...)``. We capture ``complete`` calls so the test can
    inspect the response that would have been sent.
    """

    request: Request
    body: bytes = b""
    response_status: int | None = None
    attrs: dict[Any, Any] = field(default_factory=dict)
    completed_response: Response | None = None
    completed_body: bytes = b""

    def complete(self, response: Response, body: bytes | None = None) -> None:
        self.response_status = response.status_code
        self.completed_response = response
        self.completed_body = body or b""


@dataclass(frozen=True, slots=True)
class _Outcome:
    status: int
    route_match: RouteMatch | None
    response: Response | None


def _route_handler(ctx: HTTPReqCtx) -> BodyHandler | None:
    ctx.complete(Response(200, [(b"content-length", b"0")]), b"")
    return None


def _build_router(routes_list: list[tuple[str, frozenset[HTTPMethod], str]]) -> Router:
    routes = Routes()
    for template, methods, _concrete in routes_list:
        for m in methods:
            routes.add(m, template, _route_handler)
    return routes.build()


def _dispatch(router: Router, method: HTTPMethod, path: str) -> _Outcome:
    request = Request(
        method=method.value.encode("ascii"),
        target=path.encode("iso-8859-1"),
        path=path.encode("iso-8859-1"),
        query_string=b"",
        headers=[],
    )
    ctx = _FakeCtx(request=request)
    handler = router.as_handler()
    handler(cast(HTTPReqCtx, ctx))
    return _Outcome(
        status=ctx.response_status or 0,
        route_match=ctx.attrs.get(RouteMatch),
        response=ctx.completed_response,
    )


# --- Helpers mirroring the source-of-truth (router.py) ----------------------

_VAR_PATTERN = re.compile(r"\{([^}]+)\}")


def _literal_prefix_len(template: str) -> int:
    """Mirror of :func:`localpost.http.router._literal_prefix_len`."""
    m = _VAR_PATTERN.search(template)
    return len(template) if m is None else m.start()


def _matches(template: str, path: str) -> bool:
    return URITemplate.parse(template).match(path) is not None


def _allow_header(response: Response) -> str | None:
    for name, value in response.headers:
        if name.lower() == b"allow":
            return value.decode("ascii")
    return None


# --- Properties -------------------------------------------------------------


class TestRouterDispatchProperties:
    @given(scenario=_scenario())
    @settings(max_examples=300, deadline=None)
    def test_dispatch_outcome_matches_spec(self, scenario):
        """200/404/405 outcome must match the spec at router.py:241-287."""
        routes, method, path = scenario
        router = _build_router(routes)
        outcome = _dispatch(router, method, path)

        matching = [(t, m) for t, m, _ in routes if _matches(t, path)]
        any_method_matches = any(method in methods for _, methods in matching)

        if not matching:
            assert outcome.status == 404
        elif any_method_matches:
            assert outcome.status == 200
        else:
            assert outcome.status == 405

    @given(scenario=_scenario())
    @settings(max_examples=300, deadline=None)
    def test_chosen_template_has_max_literal_prefix(self, scenario):
        """On a 200, the chosen template has maximal literal prefix among method-supporting matchers.

        Mirrors the sort at router.py:209-213. Stable-sort means ties resolve
        to insertion order; we assert ``==`` on the prefix length, which is
        the only spec-level property without depending on insertion order.
        """
        routes, method, path = scenario
        router = _build_router(routes)
        outcome = _dispatch(router, method, path)

        if outcome.status != 200:
            assume(False)

        assert outcome.route_match is not None
        chosen = outcome.route_match.matched_template.template
        method_supporting = [t for t, m, _ in routes if _matches(t, path) and method in m]
        assert chosen in method_supporting
        assert _literal_prefix_len(chosen) == max(_literal_prefix_len(t) for t in method_supporting)

    @given(scenario=_scenario())
    @settings(max_examples=300, deadline=None)
    def test_405_allow_header_is_sorted_method_union(self, scenario):
        """On a 405, the Allow header is the sorted union of all matching templates' methods.

        Mirrors router.py:281-285 (single-route 405 uses the route's pre-built
        Allow; multi-route 405 unions methods on the fly).
        """
        routes, method, path = scenario
        router = _build_router(routes)
        outcome = _dispatch(router, method, path)

        if outcome.status != 405:
            assume(False)

        assert outcome.response is not None
        allow = _allow_header(outcome.response)
        assert allow is not None

        union: set[HTTPMethod] = set()
        for t, m, _ in routes:
            if _matches(t, path):
                union |= m
        expected = ", ".join(hm.value for hm in sorted(union, key=lambda hm: hm.value))
        assert allow == expected

    @given(scenario=_scenario())
    @settings(max_examples=300, deadline=None)
    def test_404_iff_no_template_matches(self, scenario):
        """404 ⇔ no template's regex matches the path. Bidirectional."""
        routes, method, path = scenario
        router = _build_router(routes)
        outcome = _dispatch(router, method, path)
        any_match = any(_matches(t, path) for t, _, _ in routes)
        assert (outcome.status == 404) == (not any_match)
