"""Tiny shared CLI helper for app modules.

Each app reads a ``--port`` (default 8000) and binds 127.0.0.1. LocalPost
apps additionally accept ``--selectors`` (default 1) for multi-selector
mode and ``--acceptor`` to switch to the dedicated-acceptor topology
(1 acceptor thread + N worker selectors).
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
    acceptor: bool
    warmup: int


def parse_args(default_port: int = 8000) -> AppArgs:
    """Port + selectors + acceptor + warmup. Used by LocalPost bench apps."""
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=default_port)
    p.add_argument("--selectors", type=int, default=1)
    p.add_argument("--acceptor", action="store_true")
    p.add_argument(
        "--warmup",
        type=int,
        default=32,
        help="Pre-spawn N worker threads before serving (default: 32).",
    )
    a = p.parse_args()
    return AppArgs(port=a.port, selectors=a.selectors, acceptor=a.acceptor, warmup=a.warmup)
