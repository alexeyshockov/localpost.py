"""Micro-benchmarks for ``URITemplate``.

Run::

    just bench-micro
    # or
    pytest benchmarks/micro/bench_uri_template.py --benchmark-only
"""

from __future__ import annotations

from localpost.http.router import URITemplate


def bench_parse_simple(benchmark) -> None:
    benchmark(URITemplate.parse, "/books/{book_id}")


def bench_parse_three_vars(benchmark) -> None:
    benchmark(URITemplate.parse, "/orgs/{org}/users/{user}/posts/{post}")


def bench_match_hit_one_var(benchmark) -> None:
    t = URITemplate.parse("/books/{book_id}")
    benchmark(t.match, "/books/00a7a2d4-18e4-11f1-899b-d33838f3bef0")


def bench_match_hit_three_vars(benchmark) -> None:
    t = URITemplate.parse("/orgs/{org}/users/{user}/posts/{post}")
    benchmark(t.match, "/orgs/anthropic/users/alex/posts/42")


def bench_match_miss(benchmark) -> None:
    t = URITemplate.parse("/books/{book_id}")
    benchmark(t.match, "/users/alex/posts/42")
