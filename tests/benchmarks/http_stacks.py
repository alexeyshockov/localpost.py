"""Tests for the HTTP stack registry + the shared filter language."""

from __future__ import annotations

import pytest

from benchmarks.macro._core.filters import collect_dim_keys, parse_filters, select_stacks
from benchmarks.macro.http.stacks import GROUPS, STACKS

_VALID_KEYS = frozenset({"name", "tags"} | collect_dim_keys(STACKS))


def _select(*, names=None, group=None, filters=()):
    return select_stacks(STACKS, GROUPS, names=names, group=group, filters=filters)


def test_stacks_registry_unique_names():
    names = [s.name for s in STACKS]
    assert len(names) == len(set(names))


def test_select_all_by_default():
    assert _select() == STACKS


def test_select_by_app():
    selected = _select(filters=["app=flask"])
    assert {s.name for s in selected} == {
        "localpost_flask",
        "flask_cheroot",
        "flask_gunicorn",
        "flask_granian",
    }


def test_select_by_backend_glob_and_selectors():
    selected = _select(filters=["backend=lp-*", "selectors=1"])
    names = {s.name for s in selected}
    assert names == {
        "localpost_h11",
        "localpost_httptools",
        "localpost_httptools_inline",
        "localpost_wsgi",
        "localpost_flask",
    }


def test_select_negation():
    selected = _select(filters=["app!=starlette"])
    assert all(s.dims["app"] != "starlette" for s in selected)
    assert len(selected) == len(STACKS) - 2


def test_select_multi_value_or():
    selected = _select(filters=["app=wsgi,starlette"])
    assert {s.dims["app"] for s in selected} == {"wsgi", "starlette"}


def test_select_pool_off():
    selected = _select(filters=["pool=false"])
    assert all(s.dims["pool"] == "false" for s in selected)
    assert len(selected) == 3  # the three inline httptools variants


def test_group_localpost():
    selected = _select(group="localpost")
    assert all(s.dims["backend"].startswith("lp-") for s in selected)


def test_group_quick_is_small():
    selected = _select(group="quick")
    assert 2 <= len(selected) <= 6


def test_group_plus_filter_ands():
    selected = _select(group="localpost", filters=["selectors=1"])
    assert all(s.dims["backend"].startswith("lp-") and s.dims["selectors"] == "1" for s in selected)


def test_explicit_names_bypass_filters():
    selected = _select(names=["flask_gunicorn"], filters=["app=starlette"])
    assert [s.name for s in selected] == ["flask_gunicorn"]


def test_unknown_filter_key_raises():
    with pytest.raises(ValueError, match="unknown filter key"):
        parse_filters(["foo=bar"], _VALID_KEYS)


def test_unknown_name_raises():
    with pytest.raises(ValueError, match="unknown stack name"):
        _select(names=["does_not_exist"])


def test_unknown_group_raises():
    with pytest.raises(ValueError, match="unknown group"):
        _select(group="not-a-group")


def test_filter_without_equals_raises():
    with pytest.raises(ValueError, match="must be 'key="):
        parse_filters(["just-a-word"], _VALID_KEYS)


def test_groups_have_predicates():
    # Smoke check: every advertised group is callable + returns a non-empty subset.
    for name, pred in GROUPS.items():
        hits = [s for s in STACKS if pred(s)]
        assert hits, f"group {name!r} is empty"
