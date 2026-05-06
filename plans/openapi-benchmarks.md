# OpenAPI framework benchmarks

Add a second macro-bench suite that compares `localpost.openapi` against
peer typed/OpenAPI Python web frameworks. Today's `benchmarks/http/`
measures **HTTP server overhead** with thin Flask/Starlette apps;
the new `benchmarks/openapi/` measures **framework overhead** —
typed signatures, schema validation, response serialization,
OpenAPI metadata cost — with the server held fixed per framework.

## 1. Layout — extract shared core

Refactor first, then add the new suite. Three siblings under `benchmarks/`:

```
benchmarks/
  _core/                  # shared runner infra
    runner.py             # spawn → readiness probe → oha → parse → kill
    types.py              # Scenario, Stack, Cell, RunReport
    pythons.py            # PYTHONS matrix (moved from http/_pythons.py)
    render.py             # markdown + HTML reports
    cli.py                # --duration / --filter / --scenarios / --pythons / --stacks / --group
  http/                   # existing suite, slimmed
    scenarios.py
    stacks.py
    runner.py             # thin entry point → benchmarks._core.cli.main(...)
    apps/
    results/
  openapi/                # new
    scenarios.py
    stacks.py
    runner.py
    apps/
    results/
```

`_core.cli.main(scenarios, stacks, apps_pkg, results_dir, ...)` parameterizes
the suite-specific bits. `apps_pkg` is the dotted prefix used by the runner
to spawn each stack as `python -m <apps_pkg>.<stack_name>`.

`Cell` keeps `stack`, `scenario`, `rps`, `p50/p90/p99`, etc., and replaces
today's hard-coded `app`/`backend`/`selectors`/`pool`/`acceptor`/`tags` with
a generic `dims: dict[str, str]`. Suite-specific dimensions:

- http suite dims: `{app, backend, selectors, pool, acceptor}`
- openapi suite dims: `{framework, server, pydantic}`

The HTML renderer reads `dims` keys and exposes them as filters generically —
the http suite's existing UI is preserved with no behavior change.

`just bench-http` stays as-is. `just bench-openapi` is added with the same
flag surface.

## 2. Stacks (v1)

| Stack ID            | Framework                           | Server                          |
|---------------------|-------------------------------------|---------------------------------|
| `localpost_openapi` | `localpost.openapi.HttpApp`         | `localpost.http` (h11)          |
| `flask_openapi`     | `flask-openapi3 ~=5.0`              | gunicorn sync, `--threads 32`   |
| `fastapi`           | FastAPI current                     | uvicorn, 1 worker               |

`flask-openapi3` was renamed to `flask-openapi` upstream; we pin the v5 line.

Pinned in a new `[dependency-groups.bench-openapi]` group so the main install
isn't polluted. Pydantic v2 is implicit via FastAPI + flask-openapi3.

Future variants (out of scope for v1): granian, httptools, multi-selector
for `localpost_openapi`. Stack-variant slots are already supported by the
shared runner.

## 3. Scenarios (v1)

Each scenario defines one wire contract; every stack implements it
idiomatically per framework.

| Scenario             | Method · Path                                | Expected | What it exercises                                     |
|----------------------|----------------------------------------------|----------|-------------------------------------------------------|
| `plaintext`          | `GET /ping`                                  | 200      | Pure dispatch — calibration anchor.                   |
| `path_param_typed`   | `GET /items/{item_id}` (int)                 | 200      | Path coercion.                                        |
| `query_validation`   | `GET /search?q=…&limit=…&offset=…`           | 200      | Query parse + constrained validation.                 |
| `body_roundtrip`     | `POST /users/{id}/profile` JSON in/out       | 200      | Schema-driven decode → normalize → encode (headline). |
| `validation_failure` | `POST /users/{id}/profile` with bad JSON     | 422      | Error-path throughput.                                |

The `body_roundtrip` payload mirrors
`benchmarks/http/scenarios.py::_PROFILE_UPDATE_BODY` so wire contracts
align across both suites for cross-checking.

For `validation_failure`, the runner needs a small tweak: today success is
`status_2xx`. Move success classification to a per-`Scenario`
`expected_status` (already on the dataclass) and rename
`status_2xx`/`status_other` on `Cell` to `status_expected`/`status_other`.
The http suite is unaffected — every existing scenario expects 200.

## 4. Workload definitions

One model schema, defined three times in idiomatic style — not shared via a
common library:

- **LocalPost**: `@dataclass` model + return-type union
  (`Profile | BadRequest[ProblemDetails]`).
- **FastAPI**: Pydantic v2 model + `response_model=Profile` +
  `HTTPException(422)` for the failure case.
- **flask-openapi3**: Pydantic v2 model + decorator-attached schema classes
  per its v5 API.

Same fields, same types, same constraints across all three — the
validation-failure body is rejected by all three for the same field reason,
so we're comparing equivalent work.

We test the **full typed cycle**, including framework-default response
serialization. No "raw FastAPI without `response_model`" variant — that
isn't a realistic deployment shape and isn't what we're measuring.

## 5. Implementation order

1. Refactor `benchmarks/http/` into `benchmarks/_core/` + thin
   `benchmarks/http/`. Verify `just bench-http --duration 5
   --stacks localpost_h11,flask_gunicorn` produces equivalent output.
2. Scaffold `benchmarks/openapi/` with `plaintext` only across all three
   stacks. Confirm runner spawns/probes/kills each.
3. Add `path_param_typed` and `body_roundtrip`.
4. Add `query_validation` and `validation_failure` (the latter requires the
   `expected_status` plumbing).
5. README under `benchmarks/openapi/` mirroring
   `benchmarks/http/README.md`. Top-level `benchmarks/README.md` updated to
   describe the split.

## 6. Caveats (carry over from http suite README)

- Single-host noise: relative ordering on the same run is what matters.
- Single-process by design: real deployments multiply by N workers; the
  relative ordering still holds.
- Not for CI gates: GitHub Actions runners are too noisy for HTTP throughput
  regression.
