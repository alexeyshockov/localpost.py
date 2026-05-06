# 0002 — h11 and httptools coexist as parallel HTTP backends

- **Status:** Accepted
- **Date:** 2026-05-07 (backfilled — decision predates the ADR practice)

## Context

The native HTTP server in `localpost.http` needs an HTTP/1.1 parser.
Two well-maintained choices in the Python ecosystem:

- **h11** — pure Python, sans-IO, pull-based events
  (`receive_data` → iterate events). Parses *and* serialises responses.
  Easy to read; portable to free-threaded builds.
- **httptools** — a thin Cython wrapper over node.js's `llhttp`.
  Push-based callbacks (`on_url`, `on_header`, `on_body`,
  `on_message_complete`). Parse-only — the user serialises responses
  themselves. Faster header parsing.

We initially shipped only h11 (pure Python is friendlier to debug and
to free-threaded CPython). Profiling under realistic JSON-API
workloads showed parser overhead as a meaningful slice of the hot
path; httptools cuts it materially.

Two viable shapes for adding httptools:

1. **Unify behind a parser Protocol** — define an abstract parser
   interface, implement it on top of both h11 and httptools, switch
   via config.
2. **Two parallel backends** — both parsers live as full
   implementations, sharing only what already factors cleanly
   (selector loop, op queue, stale-conn sweep, shutdown
   coordination).

Forces:

- The two parsers don't fit one shape. h11 is pull-events
  parse+serialise; httptools is push-callbacks parse-only. Forcing
  one Protocol over both either hides h11's serialiser (we'd write
  our own — duplicating h11's work) or papers over httptools's
  callback model (we'd buffer events into a fake pull queue —
  defeating the perf gain).
- The shared concerns (selector, accept policy, shutdown) genuinely
  factor — they're already in `_base.py`. The non-shared concerns
  (parser drive loop, response framing) genuinely don't.
- We don't need to swap parsers at runtime. The choice is per-server
  via `ServerConfig.backend`.

## Decision

Two parallel backends. They share `_base.py` (selector, op queue,
listening socket, stale-conn sweep, shutdown). They each have their
own connection class, parser drive loop, and response writer.
`ServerConfig.backend` selects between them at server construction.

There is **no** abstract parser Protocol mediating between the two.
Code that wants to compose with both backends does so by depending
only on the neutral `Request` / `NativeResponse` types in
`localpost.http`, which both backends populate.

## Consequences

- h11 stays the default — pure Python, no extra deps. Users opt in
  to httptools via the `[http-fast]` extra.
- The httptools backend has initial-scope limitations (`Content-Length`
  response bodies only, HTTP Upgrade returns 400) — documented in
  [docs/design/server-backends.md](../design/server-backends.md).
  These are follow-ups, not architectural constraints.
- Adding a third backend (e.g. picohttpparser) means writing another
  full backend, not implementing a Protocol. That's the cost we
  accept for not paying the abstraction tax on the two we have.
- Hot-path code paths are duplicated across the two backends. The
  duplication is finite (request reading, response writing) and the
  shared base prevents drift in everything else.
