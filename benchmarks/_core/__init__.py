"""Shared infrastructure for the macro benchmark suites.

Each suite (``benchmarks/http/``, ``benchmarks/openapi/``) defines its own
scenarios, stacks, and ``apps/`` modules but reuses this package for the
runner, types, CLI, and report rendering.
"""
