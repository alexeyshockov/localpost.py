"""Macro benchmark suites.

Each suite (``http``, ``openapi``) drives a real HTTP load (oha) against a
spawned subprocess running one of its ``apps/`` modules. Shared
infrastructure — runner, filter language, types, CLI — lives in ``_core``.
``_setup.py`` syncs the shared bench venvs.
"""
