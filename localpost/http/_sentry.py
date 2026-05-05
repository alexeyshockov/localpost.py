"""Shared helpers for the Sentry tracing wrappers.

Used by :mod:`localpost.http.router_sentry` and
:mod:`localpost.http.flask_sentry`. Not part of the public API.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import sentry_sdk
from sentry_sdk.tracing import Span


@contextmanager
def request_transaction(
    *,
    op: str,
    name: str,
    source: str,
    method: str,
    url: str,
) -> Iterator[Span]:
    """Open a Sentry isolation scope + transaction for an HTTP request.

    Sets standard request tags (``http.method``, ``http.url``); callers
    record the response status via ``tx.set_http_status`` before exit.
    """
    with sentry_sdk.isolation_scope(), sentry_sdk.start_transaction(op=op, name=name, source=source) as tx:
        tx.set_tag("http.method", method)
        tx.set_data("http.url", url)
        yield tx
