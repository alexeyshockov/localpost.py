# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

### Added

### Changed

### Removed

## [0.4.0] - 2025-03-30

### Added

- `HostedService` class, to represent a named hosted service
- Hosted service middlewares: start_timeout, stop_timeout, and lifespan
- Ability to combine multiple hosted services (`+` operator)
- Ability to wrap a hosted service (or a set of services) by another one (`>>` operator)
- `Host.state` (similar to `ServiceLifetime.state`)

### Changed

- `EventView.__bool__()` instead of `EventView.is_set()`
- `app_host.AppService` instead of `app_host.HostedService`

## [0.3.1] - 2025-03-13

### Added

- More tests

## [0.3.0] - 2025-03-11

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
