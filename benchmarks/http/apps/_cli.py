"""Tiny shared CLI helper for app modules.

Each app reads a ``--port`` (default 8000) and binds 127.0.0.1. LocalPost
apps additionally accept ``--selectors`` (default 1) for multi-selector
mode.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass


def parse_port(default: int = 8000) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=default)
    return p.parse_args().port


@dataclass(slots=True, frozen=True)
class AppArgs:
    port: int
    selectors: int


def parse_args(default_port: int = 8000) -> AppArgs:
    """Port + selectors. Used by LocalPost bench apps."""
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=default_port)
    p.add_argument("--selectors", type=int, default=1)
    a = p.parse_args()
    return AppArgs(port=a.port, selectors=a.selectors)
