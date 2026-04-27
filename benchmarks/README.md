# LocalPost benchmarks

Two independent suites:

- **`http/`** — macro HTTP load benchmarks. Compare LocalPost against peer
  servers (Gunicorn, Cheroot, Granian, Uvicorn) on a fixed Flask / Starlette
  workload using [`oha`](https://github.com/hatoo/oha) as the load generator.
  Goal: publishable comparison numbers.
- **`micro/`** — `pytest-benchmark` micro-benchmarks for `URITemplate` and
  `Router`. Goal: catch perf regressions in the deterministic core.

Both are kept out of the default test run (`just tests`).

## Quick start

```bash
# macro (HTTP load) — 8 stacks × 3 scenarios at 20s each = ~8 minutes
brew install oha   # one-time prereq
just bench-http

# faster sanity sweep
just bench-http --duration 5 --stacks localpost_native,flask_gunicorn

# micro (pytest-benchmark)
just bench-micro
```

## What's compared (macro)

Three "app types", each implementing the same three scenarios:

| App type   | Stacks                                                                                    |
|------------|-------------------------------------------------------------------------------------------|
| Native     | `localpost_native`                                                                        |
| WSGI/Flask | `localpost_wsgi`, `localpost_flask`, `flask_cheroot`, `flask_gunicorn`, `flask_granian`   |
| ASGI       | `starlette_uvicorn`, `starlette_granian`                                                  |

Scenarios — defined in [`http/scenarios.py`](http/scenarios.py):

| Scenario     | Method | Path             | Concurrency |
|--------------|--------|------------------|-------------|
| `plaintext`  | GET    | `/ping`          | 64          |
| `path_param` | GET    | `/hello/{name}`  | 64          |
| `json_post`  | POST   | `/echo`          | 32          |

All servers are configured to be roughly comparable — single process, sized
worker pool (`max_concurrency=32` for LocalPost, `--threads 32` for
Gunicorn/Granian-WSGI, etc.). We're measuring server overhead, not the
multiplicative effect of more processes.

### How a run works

`benchmarks/http/runner.py`, for each (stack, scenario):
1. `subprocess.Popen` the stack as `python -m benchmarks.http.apps.<stack>`.
2. Poll `127.0.0.1:<port>` with TCP connect until ready (≤ 10 s).
3. Fire `oha --no-tui -j -z <duration>s -c <concurrency> ...`.
4. Parse JSON → RPS, p50/p90/p99, status histogram.
5. SIGTERM the stack, wait, move on.

Output:
- `http/results/latest.json` — raw cells (re-parseable).
- `http/results/RESULTS.md`  — markdown summary, one table per scenario.

### Caveats

- **Single-host noise.** Numbers are sensitive to CPU thermals, kernel
  scheduling, other processes. Re-run if a cell looks anomalous; consider
  running with everything else closed. The relative ordering on the *same*
  run is what matters; absolute RPS will shift run-to-run.
- **Not for CI gates.** GitHub Actions runners are too noisy for HTTP
  throughput regression gates. Run macro benchmarks locally / on a
  dedicated machine.
- **Single process by design.** All servers configured `workers=1` to
  isolate the server-layer overhead. Real deployments multiply by N
  workers; the relative ordering still holds.

## What's compared (micro)

`pytest-benchmark`-driven, runs in-process:

- `bench_uri_template.py` — `URITemplate.parse`, `.match` (hit / miss /
  multi-var).
- `bench_router.py` — `Routes.build`, `Router.wsgi` dispatch (literal hit,
  parameterised hit, 404, 405).

Use `--benchmark-compare` / `--benchmark-autosave` to compare against a
saved baseline. See [pytest-benchmark
docs](https://pytest-benchmark.readthedocs.io/en/latest/comparing.html).

```bash
# Save a baseline before changing code
just bench-micro --benchmark-autosave

# Then later, compare
just bench-micro --benchmark-compare --benchmark-compare-fail=mean:10%
```

Micro-benchmarks are **not** wired to a CI check. If you want CI-stable
regression gates later, swap `pytest-benchmark` for
[`pytest-codspeed`](https://docs.codspeed.io/) — it runs the same fixtures
under deterministic instrumentation on Codspeed's infra. No code changes
beyond the dependency.

## Adding a new stack / scenario

- **Stack:** drop a `python -m`-runnable module under `http/apps/` that
  takes `--port`, binds 127.0.0.1, and serves the routes from
  `scenarios.py`. Add the module name to `STACKS` in `http/runner.py`.
- **Scenario:** add a `Scenario(...)` to `SCENARIOS` in `scenarios.py`,
  then implement the route in every app (typically by extending
  `_flask_app.py` and `_starlette_app.py`).

## Roadmap (not committed)

- A FastAPI app variant — same engine as Starlette, but pulls in Pydantic
  overhead. Useful as a third app type.
- Streaming + keep-alive scenarios.
- Plotly-rendered HTML chart from `latest.json`.
- A scheduler micro-bench suite (trigger composition, `ScheduledTask`
  setup/teardown).
