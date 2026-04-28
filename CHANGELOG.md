# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

### Added

- `localpost.http.thread_pool_handler` — async context manager that wraps
  any `RequestHandler` so it runs on a worker thread. Compose explicitly
  with `http_server` when you need a worker pool; immediate handlers
  (including a `Router`'s 404/405 path) stay on the selector thread.
- `just deadcode` — vulture-based dead-code finder, configured in
  `pyproject.toml` (`[tool.vulture]`).

### Changed

- `localpost.http._service.http_server` no longer accepts `max_concurrency`
  and no longer owns a worker pool. Wrap your handler with
  `thread_pool_handler` to opt back into worker dispatch.
- `localpost.http.flask.flask_server` and `localpost.http.wsgi_server`
  drop their `max_concurrency` kwarg for the same reason — wrap with
  `thread_pool_handler` if you need a pool (typical for blocking WSGI
  / Flask apps).
- **Experimental sub-packages moved.** `localpost.consumers` →
  `localpost.experimental.consumers`; `localpost.openapi` →
  `localpost.experimental.openapi`. The `experimental` segment in every
  import path is the new stability marker — README notes alone were too
  easy to miss. APIs themselves are unchanged.

### Removed

## [0.6.0] - 2026-02-22

Complete rewrite of the hosting system, to simplify it and make it more robust.

### Fixed

### Added

- `localpost.http` — selectors-based non-async HTTP server

### Changed

### Removed

- `localpost.flow` — too complicated

## [0.5.0] - 2025-07-18

### Added

- `localpost.consumers.stream` for in-memory queues
- `localpost.hosting.services.hypercorn` for Hypercorn HTTP server
- `localpost.debug` context manager, to simplify debugging
- More tests

### Changed

- `localpost.consumers.kafka` reworked
- `localpost.consumers.sqs` reworked (now with both `boto3` and `aioboto3` support)

## [0.4.0] - 2025-06-23

### Fixed

- `UvicornService` crashes the whole app if the server fails to start

### Added

- `HostedService` class, to represent a named hosted service
- Hosted service middlewares: start_timeout, shutdown_timeout, and lifespan
- Ability to combine multiple hosted services (`+` operator)
- Ability to wrap a hosted service (or a set of services) by another one (`>>` operator)
- `Host.state` (similar to `ServiceLifetime.state`)
- More tests

### Changed

- `EventView.__bool__()` in addition to `EventView.is_set()`
- `AppHost` reworked (simplified)
- `localpost.flow_ops` merged into `localpost.flow`

### Removed

- `localpost.scheduler.serve()` & `localpost.scheduler.aserve()` (just use `Host` instead)

## [0.3.0] - 2025-03-12

### Changed

- Renamed (from `justscheduleit` to `localpost`)
- Hosting reworked: Host (one service) & AppHost (many services)
- Scheduler: internals reworked completely

## [0.2.0] — 2024-10-03

### Added

- Batch trigger

### Fixed

- Safer async generators handling

## [0.1.0] — 2024-09-30

### Added

- Hosting foundation for the scheduler
- Scheduler itself
