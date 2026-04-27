"""Tiny shared CLI helper for app modules.

Each app reads a ``--port`` (default 8000) and binds 127.0.0.1.
"""

from __future__ import annotations

import argparse


def parse_port(default: int = 8000) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=default)
    return p.parse_args().port
