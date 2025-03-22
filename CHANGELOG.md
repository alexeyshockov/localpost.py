# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

### Added

### Changed

### Removed

## [0.4.0] - 2025-03-23

### Added

- `Host.use()` to apply middlewares
- Ability to combine multiple hosts (`+` operator)

### Changed

- `EventView.__bool__()` instead of `EventView.is_set()` 

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
