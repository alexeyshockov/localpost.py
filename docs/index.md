# LocalPost docs

Concept docs and deeper dives that don't fit in the per-module READMEs.
Layout is mkdocs-ready (one section per top-level dir); a static site
generator can be wired up later without moving anything.

## Modules

User-facing references live next to the code:

- [`localpost.hosting`](../localpost/hosting/README.md)
- [`localpost.scheduler`](../localpost/scheduler/README.md)
- [`localpost.http`](../localpost/http/README.md)
- [`localpost.openapi`](../localpost/openapi/README.md)
- [`localpost.di`](../localpost/di/README.md)

## Design notes

Architectural decisions and contract explanations — read these when
building on top of `localpost` or extending an existing module.

- [Request body handling across transports](design/request-body-handling.md) —
  what `ctx.receive(size)` does on the native server, WSGI, ASGI, and
  RSGI; how the pre-buffer / streaming distinction is a transport
  choice, not a Protocol switch.
- [Deployment topologies](design/deployment-topologies.md) — uvicorn /
  hypercorn run as hosted services *inside* `run_app`; Granian is a
  process supervisor that runs the host *inside* its workers. Why the
  two cases are asymmetric and what `HostRSGIApp` does about it.

## Plans

Work-in-progress design plans live at the repo root under
[`plans/`](../plans/) — e.g. RSGI deployment, the dynamic worker pool.
They're forward-looking; once a plan lands, its rationale moves into a
design note here and the plan file is removed.
