# LocalPost OpenAPI framework benchmark

Compares `localpost.openapi` against peer typed/OpenAPI Python web
frameworks under one fixed workload, single-process, single-host.

This is the **framework** bench. The sibling
[`benchmarks/macro/http/`](../http/README.md) measures **server** overhead
with a thin Flask/Starlette app on top — different question, different
suite.

## Quick start

```bash
brew install oha   # one-time prereq
just bench-deps    # provision .venv-bench/<py>/ for every interpreter
just bench-openapi
```

```bash
# Faster sanity sweep
just bench-openapi --duration 5

# Restrict to one stack
just bench-openapi --stacks fastapi

# Restrict to one scenario
just bench-openapi --scenarios body_roundtrip

# Filter by dim
just bench-openapi --filter framework=localpost
just bench-openapi --filter 'framework=fastapi,localpost' --filter schema=pydantic
```

Output lands in `benchmarks/macro/openapi/results/<UTC-timestamp>/<python-label>/`:

- `results.json` — raw cells (re-parseable).
- `RESULTS.md`   — markdown summary, one table per scenario.
- `RESULTS.html` — interactive view with sortable tables and dim filters.

## What's compared

Five stacks — the three `localpost.openapi` flavours plus one peer framework each:

| Stack ID                       | Framework             | Server                       | Schema   |
|--------------------------------|-----------------------|------------------------------|----------|
| `localpost_openapi`            | `localpost.openapi`   | `localpost.http` (h11), sync | msgspec  |
| `localpost_openapi_async`      | `localpost.openapi`   | uvicorn (1 worker), async    | msgspec  |
| `localpost_openapi_granian`    | `localpost.openapi`   | granian (1 worker, RSGI), async | msgspec  |
| `flask_openapi`                | `flask-openapi` (v5)  | gunicorn sync (32 threads)   | pydantic |
| `fastapi`                      | FastAPI               | uvicorn (1 worker)           | pydantic |

Single-process by design — we measure framework-layer overhead, not the
multiplicative effect of more workers. All servers configured to be
roughly comparable (`max_concurrency=32` for LocalPost,
`--threads 32` for Gunicorn, etc.).

The three LocalPost stacks share `framework=localpost`; the `server`
dim discriminates them in result tables (`lp-h11` / `uvicorn` /
`granian`). What each pair anchors:

- **async vs sync** (`localpost_openapi_async` vs `localpost_openapi`)
  — what dropping the event loop costs or saves.
- **RSGI vs ASGI** (`localpost_openapi_granian` vs
  `localpost_openapi_async`) — same handler, same uvicorn-class server
  on both sides; the only delta is the wire bridge (single eager
  `response_bytes` on RSGI vs ASGI's two-event start+body).
- **localpost vs FastAPI** (`localpost_openapi_granian` vs `fastapi`)
  — same Granian-class deployment story, different framework.

`flask-openapi3` was renamed to `flask-openapi` for the v5 line; the
`bench-openapi` dependency group pins `flask-openapi >=5.0.0rc1`.

## Scenarios

Defined in [`scenarios.py`](scenarios.py). Each app implements identical
wire contracts:

| Scenario             | Method · Path                              | Expected | What it exercises                                 |
|----------------------|--------------------------------------------|---------:|---------------------------------------------------|
| `plaintext`          | `GET /ping`                                | 200      | Pure dispatch — calibration anchor.               |
| `path_param_typed`   | `GET /items/42` (`item_id: int`)           | 200      | Path coercion.                                    |
| `query_validation`   | `GET /search?q=hello&limit=10&offset=0`    | 200      | Query parse + type coercion.                      |
| `body_roundtrip`     | `POST /users/42/profile` JSON in/out       | 200      | Schema-driven decode → normalize → encode (headline). |
| `validation_failure` | `POST /users/42/profile` (missing fields)  | 422      | Error-path throughput.                            |

Each app defines its own model classes idiomatically — dataclasses for
LocalPost, Pydantic v2 for FastAPI and flask-openapi. Same fields, same
types; we're comparing framework overhead on equivalent work, not
business logic.

For `validation_failure`: LocalPost's body resolver returns
`BadRequest` (400) by default, while FastAPI and flask-openapi v5
return 422. To keep the comparison apples-to-apples, all three
LocalPost bench apps attach a small middleware that remaps
400 → 422 on the profile endpoint. The framework default is unchanged.

## Adding a new stack / scenario

- **Stack:** drop a `python -m`-runnable module under `apps/` that
  takes `--port`, binds 127.0.0.1, and serves the routes from
  `scenarios.py`. Add a `Stack(...)` to `STACKS` in `stacks.py`.
- **Scenario:** add a `Scenario(...)` to `SCENARIOS` in `scenarios.py`,
  then implement the route in every app.

## Caveats

- **Single-host noise.** Numbers are sensitive to CPU thermals, kernel
  scheduling, other processes. Re-run if a cell looks anomalous. The
  relative ordering on the *same* run is what matters; absolute RPS
  will shift run-to-run.
- **Not for CI gates.** GitHub Actions runners are too noisy for HTTP
  throughput regression gates. Run macro benchmarks locally.
- **Single process by design.** Real deployments multiply by N
  workers; the relative ordering still holds.
