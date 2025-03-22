#!/usr/bin/env -S just --justfile

default:
    just --list

deps:
    pdm install --group :all

deps-upgrade:
    pdm lock --group :all -v
    pdm sync --group :all --clean

check: check-style types

[doc("Check types (using both PyRight and MyPy)")]
types:
    -pyright --pythonpath $(which python) localpost
    -mypy --pretty --python-executable $(which python) localpost

[doc("Check types (including examples and tests)")]
types-all: types
    pyright --pythonpath $(which python) examples tests

[doc("Check that the public API is correctly typed")]
type-coverage:
    pyright --pythonpath $(which python) --verifytypes localpost localpost/*

check-style:
    ruff check localpost
    ruff check examples tests

format:
    ruff check --fix localpost
    ruff format localpost

format-all: format
    ruff check --fix examples tests
    ruff format examples tests

[doc("Inverse dependency tree for a package, to understand why it is installed")]
why package:
    uv tree --invert --package {{ package }}

test:
    pytest --cov-report=term --cov-report=xml --cov-branch --cov -v
