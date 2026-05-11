# LocalPost

A small async Python framework for long-running processes:

- **hosting** — service lifecycle + orchestration (start / stop / signals)
- **scheduler** — in-process, composable task scheduler
- **http** — lightweight sync HTTP/1.1 server (h11, ~400 LOC)
- **di** — `.NET`-style scoped IoC container

Built on [AnyIO](https://anyio.readthedocs.io/) — runs on **asyncio** *and*
**Trio**. Python 3.12+.

LocalPost is not a monolith: each module is usable on its own. Pick what you
need.

## Install

```bash
pip install localpost
```

Optional extras turn on individual subsystems:

| Extra            | Adds                                                    |
| ---------------- | ------------------------------------------------------- |
| `[scheduler]`    | `humanize`, `pytimeparse2` — string durations           |
| `[cron]`         | `croniter` — cron-expression trigger                    |
| `[http]`         | `h11` — HTTP server                                     |
| `[http-fast]`    | `httptools` — alternative C-based parser                |
| `[http-compress]`| `brotli` — Brotli compression (gzip is stdlib)          |
| `[openapi]`      | `msgspec` — typed HTTP framework with OpenAPI 3.2       |
| `[rsgi]`         | `granian` — RSGI bridge for Granian deployments         |

## Where to next

- **[Getting started](getting-started.md)** — the 60-second tour.
- **Modules** — per-module references:
  [hosting](modules/hosting.md),
  [scheduler](modules/scheduler.md),
  [http](modules/http.md),
  [di](modules/di.md),
  [openapi](modules/openapi.md).
- **Design notes** — how the system works today and why
  ([connection model](design/connection-model.md),
  [threading topologies](design/threading-topologies.md), …).
- **[ADRs](adr/index.md)** — dated, immutable records of non-trivial design
  decisions.

## Status

Beta — actively developed. All four core modules have settled public APIs and
are not expected to break in patch or minor releases. See the
[CHANGELOG](https://github.com/alexeyshockov/localpost.py/blob/main/CHANGELOG.md)
for history.

MIT licensed.
