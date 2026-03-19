# Inversion of Control, made simple

Inspired by .NET DependencyInjection

## Design goals (and non-goals)

- no async for now (maybe in the future)
- Scope is defined by it's type
- Only one registration per type
- Service Locator, no @inject decorator as in other DI-oriented frameworks
    - But constructor wiring is supported

## TODO

- multiple services for the same type... Like IHostedService in .NET ?
    - it creates confusion also, as you now can resolve first or list...
