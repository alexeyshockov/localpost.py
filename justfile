#!/usr/bin/env -S just --justfile

default:
    just --list

deps:
    uv sync --all-groups --all-extras

deps-upgrade:
    uv lock --upgrade
    uv sync --all-groups --all-extras

[doc("Check types (using ty)")]
types:
    -ty check localpost

[doc("Check types strictly (using ty)")]
types-strict:
    -ty check localpost

[doc("Check types (including examples and tests)")]
types-all: types
    -ty check examples tests

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
    -ty check {{ file }}

tests:
    pytest --cov-report=term --cov-report=xml --cov-branch --cov -v

unit-tests:
    pytest -m "not integration" --cov-report=term --cov-branch --cov -v

integration-tests:
    pytest -m "integration" -n auto -v

[doc("Inverse dependency tree for a package, to understand why it is installed")]
why package:
    uv tree --invert --package {{ package }}
