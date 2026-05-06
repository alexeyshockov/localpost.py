"""Filter language + stack selection for macro benchmarks.

Filter language (parsed by :func:`parse_filters`):

* ``key=value`` — exact match. Multiple ``--filter`` flags AND together.
* ``key=a,b`` — comma list inside a key is OR.
* ``key=lp-*`` — glob via ``*`` / ``?`` (fnmatch).
* ``key!=value`` — negation (combines with comma + glob).
* Special keys: ``name`` (stack name), ``tags`` (multi-valued).
  Other keys are looked up in ``Stack.dims``.

Each suite passes its own ``STACKS`` tuple and ``GROUPS`` mapping into
:func:`select_stacks` — the language and resolution logic are shared.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from benchmarks._core.types import Stack


@dataclass(frozen=True, slots=True)
class _Filter:
    key: str
    values: tuple[str, ...]
    """Each entry is an fnmatch pattern."""
    negate: bool


def parse_filters(specs: Iterable[str], valid_keys: frozenset[str]) -> tuple[_Filter, ...]:
    """Parse ``key=v[,v]`` / ``key!=v[,v]`` filter strings.

    Raises ``ValueError`` with a helpful message on bad input.
    """
    out: list[_Filter] = []
    for raw in specs:
        spec = raw.strip()
        if not spec:
            continue
        negate = False
        if "!=" in spec:
            key, _, values = spec.partition("!=")
            negate = True
        elif "=" in spec:
            key, _, values = spec.partition("=")
        else:
            raise ValueError(f"--filter must be 'key=value' or 'key!=value', got: {spec!r}")
        key = key.strip()
        if key not in valid_keys:
            raise ValueError(f"unknown filter key {key!r}; valid keys: {sorted(valid_keys)}")
        items = tuple(v.strip() for v in values.split(",") if v.strip())
        if not items:
            raise ValueError(f"filter {spec!r} has no values")
        out.append(_Filter(key=key, values=items, negate=negate))
    return tuple(out)


def _stack_field(stack: Stack, key: str) -> tuple[str, ...]:
    if key == "name":
        return (stack.name,)
    if key == "tags":
        return tuple(sorted(stack.tags))
    if key in stack.dims:
        return (stack.dims[key],)
    return ()


def _match_filter(stack: Stack, f: _Filter) -> bool:
    haystack = _stack_field(stack, f.key)
    matched = any(fnmatch.fnmatchcase(h, pat) for h in haystack for pat in f.values)
    return not matched if f.negate else matched


def collect_dim_keys(stacks: Iterable[Stack]) -> frozenset[str]:
    keys: set[str] = set()
    for s in stacks:
        keys.update(s.dims)
    return frozenset(keys)


def select_stacks(
    all_stacks: tuple[Stack, ...],
    groups: dict[str, Callable[[Stack], bool]],
    *,
    names: Iterable[str] | None = None,
    group: str | None = None,
    filters: Iterable[str] = (),
) -> tuple[Stack, ...]:
    """Resolve which stacks to run.

    Resolution order:

    1. If ``names`` is given, return those stacks verbatim (escape hatch;
       ``group`` and ``filters`` are ignored).
    2. Start from ``groups[group]`` if set, else all of ``all_stacks``.
    3. AND every parsed filter on top.

    Raises ``ValueError`` for unknown names / groups / filter keys.
    """
    by_name = {s.name: s for s in all_stacks}
    if names:
        unknown = [n for n in names if n not in by_name]
        if unknown:
            raise ValueError(f"unknown stack name(s): {unknown}. Known: {sorted(by_name)}")
        return tuple(by_name[n] for n in names)

    pool: tuple[Stack, ...] = all_stacks
    if group is not None:
        if group not in groups:
            raise ValueError(f"unknown group {group!r}; known: {sorted(groups)}")
        pred = groups[group]
        pool = tuple(s for s in pool if pred(s))

    valid_keys = frozenset({"name", "tags"} | collect_dim_keys(all_stacks))
    parsed = parse_filters(filters, valid_keys)
    if parsed:
        pool = tuple(s for s in pool if all(_match_filter(s, f) for f in parsed))
    return pool
