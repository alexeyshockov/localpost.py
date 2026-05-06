# LocalPost benchmarks

Three independent suites:

- **[`http/`](http/README.md)** — macro HTTP load benchmarks. Compare
  LocalPost's HTTP server against peer servers (Gunicorn, Cheroot,
  Granian, Uvicorn) on a fixed Flask / Starlette workload using
  [`oha`](https://github.com/hatoo/oha) as the load generator. Measures
  **HTTP server** overhead.
- **[`openapi/`](openapi/README.md)** — macro framework benchmarks.
  Compare `localpost.openapi` against peer typed/OpenAPI frameworks
  (FastAPI, flask-openapi v5) on a shared workload — typed handlers,
  schema validation, response serialization. Measures **framework**
  overhead.
- **[`micro/`](micro/)** — `pytest-benchmark` micro-benchmarks for
  `URITemplate` and `Router`. Catches perf regressions in the
  deterministic core.

The two macro suites share the runner, types, filter language, and
report writers via [`_core/`](_core/). Each defines its own scenarios,
stacks, and `apps/`.

All three are kept out of the default test run (`just tests`).

## Quick start

```bash
brew install oha   # one-time prereq for macro suites
just bench-deps    # provision .venv-bench/<py>/ for every interpreter

# Macro HTTP server bench
just bench-http
just bench-http --duration 5 --stacks localpost_h11,flask_gunicorn

# Macro OpenAPI framework bench
just bench-openapi
just bench-openapi --duration 5 --stacks fastapi,localpost_openapi

# Micro (pytest-benchmark)
just bench-micro
```

Per-suite docs: [`http/README.md`](http/README.md),
[`openapi/README.md`](openapi/README.md).

## Shared CLI surface

Both macro runners share these flags via
[`benchmarks/_core/cli.py`](_core/cli.py):

- `--duration N`            — seconds per cell (default 20).
- `--stacks a,b`            — verbatim stack name list (escape hatch).
- `--group <name>`          — named preset (e.g. `quick`, `localpost`).
- `--filter key=val`        — dim filter (repeatable, AND together).
  Supports glob (`backend=lp-*`), comma (`app=flask,starlette`),
  negation (`app!=starlette`).
- `--scenarios a,b`         — restrict to specific scenarios.
- `--pythons name=path,...` — override the bench Python matrix
  (default: every entry in
  [`benchmarks._core.pythons.PYTHONS`](_core/pythons.py)).

## Micro suite

`pytest-benchmark`-driven, runs in-process:

- `bench_uri_template.py` — `URITemplate.parse`, `.match` (hit / miss /
  multi-var).
- `bench_router.py` — `Routes.build`, `Router.wsgi` dispatch (literal
  hit, parameterised hit, 404, 405).

```bash
# Save a baseline before changing code
just bench-micro --benchmark-autosave

# Then later, compare
just bench-micro --benchmark-compare --benchmark-compare-fail=mean:10%
```

Micro-benchmarks are **not** wired to a CI check. If you want CI-stable
regression gates later, swap `pytest-benchmark` for
[`pytest-codspeed`](https://docs.codspeed.io/) — it runs the same
fixtures under deterministic instrumentation on Codspeed's infra. No
code changes beyond the dependency.

## Caveats (macro)

- **Single-host noise.** Numbers are sensitive to CPU thermals, kernel
  scheduling, other processes. Re-run if a cell looks anomalous. The
  relative ordering on the *same* run is what matters; absolute RPS
  will shift run-to-run.
- **Not for CI gates.** GitHub Actions runners are too noisy for HTTP
  throughput regression gates. Run macro benchmarks locally.
- **Single process by design.** Real deployments multiply by N
  workers; the relative ordering still holds.
