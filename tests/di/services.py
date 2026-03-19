from collections.abc import Generator
from dataclasses import dataclass

import pytest

from localpost.di._services import (
    AppContext,
    ServiceNotRegisteredError,
    ServiceProvider,
    ServiceRegistry,
    _factory_for_type,
    current_provider,
    scope,
    service_provider,
)

# --- Test fixtures (service classes) ---


@dataclass
class Config:
    host: str
    port: int


class Database:
    def __init__(self, config: Config):
        self.config = config


class UserRepository:
    def __init__(self, db: Database):
        self.db = db


class NoAnnotations:
    def __init__(self, something):
        self.something = something


# --- Tests ---


class TestFactoryFor:
    def test_resolves_constructor_params(self):
        registry = ServiceRegistry()
        registry.register_value(Config(host="localhost", port=5432))
        registry.register(Database)

        with registry.app_scope() as provider:
            db = provider.resolve(Database)
            assert isinstance(db, Database)
            assert db.config.host == "localhost"
            assert db.config.port == 5432

    def test_raises_on_missing_annotation(self):
        with pytest.raises(TypeError, match="has no type annotation"):
            _factory_for_type(NoAnnotations)

    def test_no_params(self):
        """A class with no __init__ params (besides self) should just be instantiated."""

        class Simple:
            pass

        factory = _factory_for_type(Simple)
        registry = ServiceRegistry()
        with registry.app_scope() as provider:
            with factory(provider) as instance:
                assert isinstance(instance, Simple)


class TestServiceProvider:
    def test_resolve_registered_value(self):
        registry = ServiceRegistry()
        config = Config(host="127.0.0.1", port=8080)
        registry.register_value(config)

        with registry.app_scope() as provider:
            resolved = provider.resolve(Config)
            assert resolved is config

    def test_resolve_with_auto_wiring(self):
        registry = ServiceRegistry()
        registry.register_value(Config(host="127.0.0.1", port=8080))
        registry.register(Database)

        with registry.app_scope() as provider:
            db = provider.resolve(Database)
            assert isinstance(db, Database)
            assert db.config.host == "127.0.0.1"

    def test_resolve_transitive_dependencies(self):
        registry = ServiceRegistry()
        registry.register_value(Config(host="localhost", port=5432))
        registry.register(Database)
        registry.register(UserRepository)

        with registry.app_scope() as provider:
            repo = provider.resolve(UserRepository)
            assert isinstance(repo, UserRepository)
            assert isinstance(repo.db, Database)
            assert repo.db.config.host == "localhost"

    def test_resolve_caches_within_scope(self):
        registry = ServiceRegistry()
        registry.register_value(Config(host="localhost", port=5432))
        registry.register(Database)

        with registry.app_scope() as provider:
            db1 = provider.resolve(Database)
            db2 = provider.resolve(Database)
            assert db1 is db2

    def test_resolve_unregistered_raises(self):
        registry = ServiceRegistry()

        with registry.app_scope() as provider:
            with pytest.raises(ServiceNotRegisteredError):
                provider.resolve(Config)

    def test_resolve_unregistered_in_nested_scope_raises(self):
        """Unregistered type should raise even when a parent scope exists."""
        outer_registry = ServiceRegistry()
        outer_registry.register_value(Config(host="outer", port=1))

        inner_registry = ServiceRegistry()
        # Database is not registered in inner_registry

        with outer_registry.app_scope():
            inner_ctx = AppContext()
            with inner_ctx.ctx, scope(inner_registry, inner_ctx) as inner_provider:
                with pytest.raises(ServiceNotRegisteredError):
                    inner_provider.resolve(Database)

    def test_getitem(self):
        registry = ServiceRegistry()
        config = Config(host="localhost", port=5432)
        registry.register_value(config)

        with registry.app_scope() as provider:
            assert provider[Config] is config

    def test_register_with_custom_factory(self):
        def custom_factory() -> Config:
            return Config(host="custom", port=9999)

        registry = ServiceRegistry()
        registry.register(Config, custom_factory)

        with registry.app_scope() as provider:
            config = provider.resolve(Config)
            assert config.host == "custom"
            assert config.port == 9999


class TestScope:
    def test_sets_context_var(self):
        registry = ServiceRegistry()
        registry.register_value(Config(host="localhost", port=5432))

        with registry.app_scope():
            # current_provider() should work inside the scope
            provider = current_provider()
            config = provider.resolve(Config)
            assert config.host == "localhost"

    def test_resets_context_var_after_exit(self):
        registry = ServiceRegistry()

        with registry.app_scope():
            pass

        with pytest.raises(RuntimeError, match="No active DI scope"):
            current_provider()

    def test_service_provider_proxy(self):
        """The module-level `service_provider` proxy should delegate to the current scope."""
        registry = ServiceRegistry()
        config = Config(host="proxy-test", port=1234)
        registry.register_value(config)

        with registry.app_scope():
            assert service_provider.resolve(Config) is config

    def test_nested_scopes(self):
        """Inner scope should shadow the outer one in the context var."""
        registry = ServiceRegistry()
        outer_config = Config(host="outer", port=1)
        inner_config = Config(host="inner", port=2)

        registry.register_value(outer_config)

        with registry.app_scope() as outer_provider:
            assert outer_provider.resolve(Config).host == "outer"

            # Create a new registry for the inner scope with a different value
            inner_registry = ServiceRegistry()
            inner_registry.register_value(inner_config)

            inner_ctx = AppContext()
            with inner_ctx.ctx:
                with scope(inner_registry, inner_ctx) as inner_provider:
                    assert inner_provider.resolve(Config).host == "inner"
                    assert current_provider().resolve(Config).host == "inner"

            # After exiting inner scope, outer is restored
            assert current_provider().resolve(Config).host == "outer"


class TestAppScopeLifecycle:
    def test_auto_close_on_scope_exit(self):
        """Services with close() registered without a factory should be auto-closed on scope exit."""
        closed = []

        class Pool:
            def __init__(self, config: Config):
                self.config = config

            def close(self):
                closed.append("pool")

        class Server:
            def __init__(self, config: Config):
                self.config = config

            def close(self):
                closed.append("server")

        registry = ServiceRegistry()
        registry.register_value(Config(host="localhost", port=5432))
        registry.register(Pool)
        registry.register(Server)

        with registry.app_scope() as provider:
            provider.resolve(Pool)
            provider.resolve(Server)
            assert closed == []

        assert "pool" in closed
        assert "server" in closed

    def test_generator_factory_cleanup(self):
        """A generator factory should run cleanup code after scope exit."""
        events: list[str] = []

        class Pool:
            def __init__(self, name: str):
                self.name = name

        def pool_factory(sp: ServiceProvider) -> Generator[Pool]:
            events.append("create")
            pool = Pool("test-pool")
            yield pool
            events.append("cleanup")

        registry = ServiceRegistry()
        registry.register(Pool, pool_factory)

        with registry.app_scope() as provider:
            pool = provider.resolve(Pool)
            assert pool.name == "test-pool"
            assert events == ["create"]

        assert events == ["create", "cleanup"]

    def test_generator_factory_with_dependencies(self):
        """A generator factory should be able to resolve dependencies from the provider."""
        closed = False

        class DBPool:
            def __init__(self, config: Config):
                self.config = config

            def close(self):
                nonlocal closed
                closed = True

        def db_pool_factory(sp: ServiceProvider) -> Generator[DBPool]:
            pool = DBPool(sp[Config])
            yield pool
            pool.close()

        registry = ServiceRegistry()
        registry.register_value(Config(host="localhost", port=5432))
        registry.register(DBPool, db_pool_factory)

        with registry.app_scope() as provider:
            pool = provider.resolve(DBPool)
            assert pool.config.host == "localhost"
            assert not closed

        assert closed
