#!/usr/bin/env -S just --justfile

default:
    just --list

deps:
    uv sync --all-groups --all-extras

deps-upgrade:
    uv lock --upgrade
    uv sync --all-groups --all-extras

[doc("Check types (using both PyRight and MyPy)")]
types:
    -basedpyright --pythonpath $(which python) localpost

[doc("Check types (using both PyRight and MyPy)")]
types-strict:
    -basedpyright --pythonpath $(which python) localpost

[doc("Check types (including examples and tests)")]
types-all: types
    -basedpyright --pythonpath $(which python) \
      examples tests

[doc("Check that the public API is correctly typed")]
type-coverage:
    basedpyright --pythonpath $(which python) --verifytypes localpost localpost/*

format:
    ruff check --fix localpost
    ruff format localpost

format-all: format
    ruff check --fix examples tests
    ruff format examples tests

check file:
    -ruff check --fix {{ file }}
    -basedpyright --pythonpath $(which python) {{ file }}

tests:
    pytest --cov-report=term --cov-report=xml --cov-branch --cov -v

unit-tests:
    pytest -m "not integration" --cov-report=term --cov-branch --cov -v

integration-tests:
    pytest -m "integration" -n auto -v

[doc("Inverse dependency tree for a package, to understand why it is installed")]
why package:
    uv tree --invert --package {{ package }}
