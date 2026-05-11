#!/usr/bin/env -S just --justfile

doctor:
    brew install uv oha ty pre-commit vulture actionslint
    uv tool install griffe

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
    -basedpyright --pythonpath "$(which python)" --verifytypes localpost --ignoreexternal localpost/*

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
    pytest --cov-report=term --cov-report=xml --cov -v

unit-tests:
    pytest -m "not integration" --cov-report=term --cov -v

integration-tests:
    pytest -m "integration" -n auto -v

[doc("Set up all venvs in the bench matrix (.venv-bench/<name>)")]
bench-deps:
    uv run python -m benchmarks.macro._setup

[doc("Refresh lock and re-sync all bench-matrix venvs")]
bench-deps-upgrade:
    uv lock --upgrade
    uv run python -m benchmarks.macro._setup

[doc("Run macro HTTP benchmarks (oha-driven, requires `brew install oha`)")]
bench-http *args:
    uv run --group bench --group dev-http \
        python -m benchmarks.macro.http.runner {{ args }}

[doc("Quick PR-time HTTP bench: representative subset of stacks")]
bench-http-quick *args:
    just bench-http --group quick --duration 10 {{ args }}

[doc("Compare Flask across all server backends")]
bench-http-flask *args:
    just bench-http --filter app=flask {{ args }}

[doc("Compare LocalPost variants only (h11/httptools, selectors, inline)")]
bench-http-localpost *args:
    just bench-http --group localpost {{ args }}

[doc("Run macro OpenAPI-framework benchmarks (LocalPost vs FastAPI vs flask-openapi3)")]
bench-openapi *args:
    uv run --group bench --group bench-openapi \
        python -m benchmarks.macro.openapi.runner {{ args }}

[doc("Run micro-benchmarks (router, URI template) via pytest-benchmark")]
bench-micro *args:
    uv run --group bench pytest benchmarks/micro/ \
        --benchmark-only \
        -o python_functions='bench_*' \
        {{ args }}

[doc("Inverse dependency tree for a package, to understand why it is installed")]
why package:
    uv tree --invert --package {{ package }}

[doc("Serve docs locally with live reload")]
docs-serve:
    uv run --all-groups --all-extras zensical serve

[doc("Build docs to ./site")]
docs-build:
    uv run --all-groups --all-extras zensical build

[doc("Find unused code with vulture (config in pyproject.toml). Pass extra args to override.")]
deadcode *args:
    vulture {{ args }}

# ---------------------------------------------------------------------------
# Release pre-flight
#
# Workflow:
#   just release-check           # current type coverage + API BC vs previous stable tag
#   <eyeball the report, update CHANGELOG.md if needed>
#   just release X.Y.Z           # (see below)
# ---------------------------------------------------------------------------

# Latest tag matching strictly ``vX.Y.Z`` (no ``b1`` / ``rc1`` / ``.dev0`` suffix).
previous_stable_tag := `git tag --sort=-v:refname --list 'v*' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -n1`

[doc("Show API breaking changes vs [base] (default: previous stable tag) using griffe")]
api-diff base=previous_stable_tag:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -z "{{ base }}" ]; then
        echo "no previous stable tag found; pass [base] explicitly" >&2
        exit 1
    fi
    echo "▸ Public API vs {{ base }} (griffe check, breaking changes only)"
    griffe check localpost -a "{{ base }}" -s . || true

[doc("Pre-release report: current type coverage + API BC vs [base]")]
release-check base=previous_stable_tag:
    @just type-coverage
    @echo
    @just api-diff {{ base }}

# ---------------------------------------------------------------------------
# Release automation
#
# Workflow:
#   just release 0.6.0           # bump to 0.6.0, commit, push, open draft GH release
#   <review draft on GitHub, click Publish>  -> triggers .github/workflows/pypi-publish.yaml
#   just release-post 0.7.0      # bump to 0.7.0.dev0, commit, push
# ---------------------------------------------------------------------------

[doc("Cut a release: bump version, commit, push, open draft GH release with CHANGELOG notes")]
release version:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -n "$(git status --porcelain)" ]; then
        echo "working tree dirty; commit or stash first" >&2
        exit 1
    fi
    if [ "$(git rev-parse --abbrev-ref HEAD)" != "main" ]; then
        echo "not on main; checkout main first" >&2
        exit 1
    fi
    notes=$(awk -v v="{{ version }}" '$0 ~ "^## \\[" v "\\]" {flag=1; next} /^## \[/ {flag=0} flag' CHANGELOG.md)
    if [ -z "$notes" ]; then
        echo "no [{{ version }}] section in CHANGELOG.md" >&2
        exit 1
    fi
    uv version {{ version }}
    git commit -am "release: {{ version }}"
    git push origin main
    gh release create "v{{ version }}" --target main --draft --title "v{{ version }}" --notes "$notes"
    echo
    echo "Draft release created. Review on GitHub and click Publish to trigger the PyPI workflow."

[doc("Open the next dev cycle: bump pyproject to <next>.dev0, commit, push")]
release-post next:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -n "$(git status --porcelain)" ]; then
        echo "working tree dirty; commit or stash first" >&2
        exit 1
    fi
    if [ "$(git rev-parse --abbrev-ref HEAD)" != "main" ]; then
        echo "not on main; checkout main first" >&2
        exit 1
    fi
    uv version {{ next }}.dev0
    git commit -am "chore: bump version to {{ next }}.dev0"
    git push origin main
