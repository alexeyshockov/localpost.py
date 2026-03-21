# Inversion of Control, made simple

Inspired by .NET DependencyInjection

## Design goals (and non-goals)

- no async for now (maybe in the future)
- Scope is defined by it's type
- Only one registration per type (oposite to .NET DependencyInjection, where it's common to register multiple implentation for an interface, like IHostedService)
- A type can be registered multiple times, once per scope type
- Service Locator (no built-in @inject decorator as in other DI-oriented frameworks)
- Automatic wiring, if a service is resolved via ServiceProvider
- Instance factory can be a generator func, to do custom startup / shutdown
- An instance can be resolved on request (default) or when the scope is entered (create_on_enter=True)
