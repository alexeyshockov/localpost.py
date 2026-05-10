# Architecture Decision Records

ADRs are dated, immutable notes about decisions made in this repo —
the *moments* and *trade-offs*, not the current shape of the code.
Once accepted, an ADR is not edited; if a decision is reversed or
refined, a new ADR is added that supersedes the previous one.

This is different from `docs/design/`: design notes describe the
system *as it currently is* and are edited freely. ADRs describe
*why we got here*.

## Format

We use the [Nygard format](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions):

- **Status** — Proposed / Accepted / Superseded by ADR-NNNN
- **Date** — when the decision was accepted
- **Context** — what's going on, what forces are in play
- **Decision** — what we're going to do
- **Consequences** — what follows, good and bad

See [`template.md`](template.md) for the boilerplate.

## When to write one

Add an ADR when:

- A non-trivial direction was picked between viable alternatives, and
  the rejected ones aren't obviously wrong.
- A constraint or invariant in the code only makes sense if you know
  the history (e.g. why two parsers coexist, why the native server
  is sync-only).
- A previously-accepted ADR is being reversed or narrowed.

Don't add an ADR for things that are self-evident from the code, or
for purely tactical decisions (file naming, refactor shape).

## Numbering

Sequential, zero-padded to four digits, starting at `0001`. Once
issued, a number is never reused — even for superseded ADRs. Slug the
title in the filename: `0001-anyio-as-async-runtime.md`.

## Records

| #    | Title                                                        | Status   |
| ---- | ------------------------------------------------------------ | -------- |
| 0001 | [AnyIO as async runtime](0001-anyio-as-async-runtime.md)     | Accepted |
| 0002 | [h11 and httptools coexist](0002-h11-httptools-coexist.md)   | Accepted |
| 0003 | [Sync-only native HTTP server](0003-sync-native-http-server.md) | Accepted |
| 0004 | [Pull-based client-disconnect detection](0004-pull-based-disconnect-detection.md) | Accepted |
| 0005 | [No idle-timeout self-exit in worker pools](0005-no-idle-timeout-for-worker-pools.md) | Accepted |
