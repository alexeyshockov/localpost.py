#!/usr/bin/env -S just --justfile

default:
    just --list

deps:
    pdm install --group :all

deps-upgrade:
    pdm lock --group :all -v
    pdm sync --group :all --clean

check: check-style check-types

[doc("Check types (using both PyRight and MyPy)")]
check-types:
    -pyright --pythonpath $(which python) localpost
    -mypy --pretty --python-executable $(which python) localpost

[doc("Check types (including examples and tests)")]
check-all-types: check-types
    pyright --pythonpath $(which python) examples tests

[doc("Check that the public API is correctly typed")]
check-type-coverage:
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

run-tests:
    pytest --cov-report=term --cov-report=xml --cov-branch --cov -v

run-tests-for-ci: run-tests
    # Dirty way to fix Sonar, otherwise it does not recognise the coverage at all
    # See https://community.sonarsource.com/t/sonar-on-github-actions-with-python-coverage-source-issue/36057
    sed -i 's#<source>/home/runner/work/'$GIT_REPO'/'$GIT_REPO'#<source>/github/workspace#g' coverage.xml
