"""Tests for the HTTP benchmark stack registry + filter parser."""

from __future__ import annotations

import pytest

from benchmarks.http.stacks import GROUPS, STACKS, parse_filters, select_stacks


def test_stacks_registry_unique_names():
    names = [s.name for s in STACKS]
    assert len(names) == len(set(names))


def test_select_all_by_default():
    assert select_stacks() == STACKS


def test_select_by_app():
    selected = select_stacks(filters=["app=flask"])
    assert {s.name for s in selected} == {
        "localpost_flask",
        "flask_cheroot",
        "flask_gunicorn",
        "flask_granian",
    }


def test_select_by_backend_glob_and_selectors():
    selected = select_stacks(filters=["backend=lp-*", "selectors=1"])
    names = {s.name for s in selected}
    assert names == {
        "localpost_h11",
        "localpost_httptools",
        "localpost_httptools_inline",
        "localpost_wsgi",
        "localpost_flask",
    }


def test_select_negation():
    selected = select_stacks(filters=["app!=starlette"])
    assert all(s.app != "starlette" for s in selected)
    assert len(selected) == len(STACKS) - 2


def test_select_multi_value_or():
    selected = select_stacks(filters=["app=wsgi,starlette"])
    assert {s.app for s in selected} == {"wsgi", "starlette"}


def test_select_pool_off():
    selected = select_stacks(filters=["pool=false"])
    assert all(not s.pool for s in selected)
    assert len(selected) == 3  # the three inline httptools variants


def test_group_localpost():
    selected = select_stacks(group="localpost")
    assert all(s.backend.startswith("lp-") for s in selected)


def test_group_quick_is_small():
    selected = select_stacks(group="quick")
    assert 2 <= len(selected) <= 6


def test_group_plus_filter_ands():
    selected = select_stacks(group="localpost", filters=["selectors=1"])
    assert all(s.backend.startswith("lp-") and s.selectors == 1 for s in selected)


def test_explicit_names_bypass_filters():
    selected = select_stacks(names=["flask_gunicorn"], filters=["app=starlette"])
    assert [s.name for s in selected] == ["flask_gunicorn"]


def test_unknown_filter_key_raises():
    with pytest.raises(ValueError, match="unknown filter key"):
        parse_filters(["foo=bar"])


def test_unknown_name_raises():
    with pytest.raises(ValueError, match="unknown stack name"):
        select_stacks(names=["does_not_exist"])


def test_unknown_group_raises():
    with pytest.raises(ValueError, match="unknown group"):
        select_stacks(group="not-a-group")


def test_filter_without_equals_raises():
    with pytest.raises(ValueError, match="must be 'key="):
        parse_filters(["just-a-word"])


def test_groups_have_predicates():
    # Smoke check: every advertised group is callable + returns a non-empty subset.
    for name, pred in GROUPS.items():
        hits = [s for s in STACKS if pred(s)]
        assert hits, f"group {name!r} is empty"
