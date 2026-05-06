# LocalPost HTTP server benchmark

Macro HTTP load benchmark — compare LocalPost's HTTP server against peer
servers (Gunicorn, Cheroot, Granian, Uvicorn) on a fixed Flask /
Starlette workload using [`oha`](https://github.com/hatoo/oha) as the
load generator.

This is the **server** bench. The sibling
[`benchmarks/openapi/`](../openapi/README.md) measures **framework**
overhead (typed handlers, schema validation) — different question,
different suite.

## Quick start

```bash
brew install oha   # one-time prereq
just bench-deps    # provision .venv-bench/<py>/ for every interpreter

# 8 stacks × 4 scenarios at 20s each = ~11 minutes
just bench-http

# faster sanity sweep
just bench-http --duration 5 --stacks localpost_h11,flask_gunicorn

# named subsets
just bench-http --group quick                # ~4 stacks, fast PR check
just bench-http --filter app=flask           # all Flask servers
just bench-http --filter 'backend=lp-*' --filter selectors=1
```

Output lands in `benchmarks/http/results/<UTC-timestamp>/<python-label>/`:

- `results.json` — raw cells (re-parseable).
- `RESULTS.md`   — markdown summary, one table per scenario.
- `RESULTS.html` — interactive view with sortable tables and dim filters.

## What's compared

Three "app types", each implementing the same four scenarios:

| App type   | Stacks                                                                                    |
|------------|-------------------------------------------------------------------------------------------|
| Native     | `localpost_h11`, `localpost_httptools`, plus their `_s4` (`SO_REUSEPORT`) and `_acceptor_s4` variants; `localpost_httptools_inline` (no pool) and its multi-selector variants |
| WSGI/Flask | `localpost_wsgi`, `localpost_flask`, `flask_cheroot`, `flask_gunicorn`, `flask_granian`   |
| ASGI       | `starlette_uvicorn`, `starlette_granian`                                                  |

Scenarios — defined in [`scenarios.py`](scenarios.py):

| Scenario         | Method | Path                        | Concurrency |
|------------------|--------|-----------------------------|-------------|
| `plaintext`      | GET    | `/ping`                     | 64          |
| `path_param`     | GET    | `/hello/{name}`             | 64          |
| `json_post`      | POST   | `/echo`                     | 32          |
| `profile_update` | POST   | `/users/{user_id}/profile`  | 32          |

`profile_update` is the more application-shaped case: route parameter,
JSON request parsing, deterministic profile normalization, three short
simulated I/O waits, and JSON response serialization.

All servers are configured to be roughly comparable — single process,
sized worker pool (`max_concurrency=32` for LocalPost, `--threads 32`
for Gunicorn/Granian-WSGI, etc.). We're measuring server overhead, not
the multiplicative effect of more processes.

## How a run works

For each (python, stack, scenario) cell, the shared runner
([`benchmarks/_core/cli.py`](../_core/cli.py)) does:

1. `subprocess.Popen` the stack as `python -m benchmarks.http.apps.<stack>`.
2. Poll `127.0.0.1:<port>` with TCP connect until ready (≤ 10 s).
3. Fire `oha --no-tui -j -z <duration>s -c <concurrency> ...`.
4. Parse JSON → RPS, p50/p90/p99, status histogram.
5. SIGTERM the stack, wait, move on.

## Adding a new stack / scenario

- **Stack:** drop a `python -m`-runnable module under [`apps/`](apps/)
  that takes `--port`, binds 127.0.0.1, and serves the routes from
  `scenarios.py`. Add a `Stack(...)` to `STACKS` in `stacks.py`.
- **Scenario:** add a `Scenario(...)` to `SCENARIOS` in `scenarios.py`,
  then implement the route in every app (typically by extending
  [`apps/_flask_app.py`](apps/_flask_app.py) and
  [`apps/_starlette_app.py`](apps/_starlette_app.py)).

## Caveats

- **Single-host noise.** Numbers are sensitive to CPU thermals, kernel
  scheduling, other processes. Re-run if a cell looks anomalous;
  consider running with everything else closed. The relative ordering
  on the *same* run is what matters; absolute RPS will shift
  run-to-run.
- **Not for CI gates.** GitHub Actions runners are too noisy for HTTP
  throughput regression gates. Run macro benchmarks locally / on a
  dedicated machine.
- **Single process by design.** All servers configured `workers=1` to
  isolate the server-layer overhead. Real deployments multiply by N
  workers; the relative ordering still holds.
