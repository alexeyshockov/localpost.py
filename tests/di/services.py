from collections.abc import Generator
from dataclasses import dataclass
from functools import partial

import pytest

from localpost._utils import set_cvar
from localpost.di._services import (
    AppContext,
    DefaultServiceProvider,
    ServiceNotRegisteredError,
    ServiceProvider,
    ServiceRegistry,
    _factory_for_type,
    current_provider,
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


class DBPoolWithClassmethod:
    def __init__(self, dsn: str):
        self.dsn = dsn

    @classmethod
    def from_config(cls, config: Config) -> "DBPoolWithClassmethod":
        return cls(dsn=f"postgres://{config.host}:{config.port}")


# --- Tests ---


class TestFactoryFor:
    def test_resolves_constructor_params(self):
        registry = ServiceRegistry()
        registry.register_instance(Config(host="localhost", port=5432))
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
        registry.register_instance(config)

        with registry.app_scope() as provider:
            resolved = provider.resolve(Config)
            assert resolved is config

    def test_resolve_with_auto_wiring(self):
        registry = ServiceRegistry()
        registry.register_instance(Config(host="127.0.0.1", port=8080))
        registry.register(Database)

        with registry.app_scope() as provider:
            db = provider.resolve(Database)
            assert isinstance(db, Database)
            assert db.config.host == "127.0.0.1"

    def test_resolve_transitive_dependencies(self):
        registry = ServiceRegistry()
        registry.register_instance(Config(host="localhost", port=5432))
        registry.register(Database)
        registry.register(UserRepository)

        with registry.app_scope() as provider:
            repo = provider.resolve(UserRepository)
            assert isinstance(repo, UserRepository)
            assert isinstance(repo.db, Database)
            assert repo.db.config.host == "localhost"

    def test_resolve_caches_within_scope(self):
        registry = ServiceRegistry()
        registry.register_instance(Config(host="localhost", port=5432))
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
        registry = ServiceRegistry()
        registry.register_instance(Config(host="outer", port=1))
        # Database is not registered

        with registry.app_scope() as provider:
            with pytest.raises(ServiceNotRegisteredError):
                provider.resolve(Database)

    def test_getitem(self):
        registry = ServiceRegistry()
        config = Config(host="localhost", port=5432)
        registry.register_instance(config)

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

    def test_register_with_classmethod_factory(self):
        registry = ServiceRegistry()
        registry.register_instance(Config(host="localhost", port=5432))
        registry.register(DBPoolWithClassmethod, DBPoolWithClassmethod.from_config)

        with registry.app_scope() as provider:
            pool = provider.resolve(DBPoolWithClassmethod)
            assert pool.dsn == "postgres://localhost:5432"

    def test_register_with_partial_factory(self):
        def make_config(host: str, port: int) -> Config:
            return Config(host=host, port=port)

        factory = partial(make_config, host="partial-host")

        registry = ServiceRegistry()
        registry.register_instance(4321, int)
        registry.register(Config, factory)

        with registry.app_scope() as provider:
            config = provider.resolve(Config)
            assert config.host == "partial-host"
            assert config.port == 4321


class TestCreate:
    def test_create_resolves_deps(self):
        """create() should resolve constructor dependencies from the provider."""
        registry = ServiceRegistry()
        registry.register_instance(Config(host="localhost", port=5432))
        registry.register(Database)

        with registry.app_scope() as provider:
            repo = provider.create(UserRepository)
            assert isinstance(repo, UserRepository)
            assert isinstance(repo.db, Database)
            assert repo.db.config.host == "localhost"

    def test_create_with_kwargs_override(self):
        """create() should allow overriding specific dependencies via kwargs."""
        registry = ServiceRegistry()
        registry.register_instance(Config(host="localhost", port=5432))

        custom_db = Database(Config(host="custom", port=9999))

        with registry.app_scope() as provider:
            repo = provider.create(UserRepository, db=custom_db)
            assert repo.db is custom_db

    def test_create_no_caching(self):
        """create() should return a new instance each time (not cached)."""
        registry = ServiceRegistry()
        registry.register_instance(Config(host="localhost", port=5432))
        registry.register(Database)

        with registry.app_scope() as provider:
            repo1 = provider.create(UserRepository)
            repo2 = provider.create(UserRepository)
            assert repo1 is not repo2


class TestScope:
    def test_sets_context_var(self):
        registry = ServiceRegistry()
        registry.register_instance(Config(host="localhost", port=5432))

        with registry.app_scope():
            # current_provider() should work inside the scope
            provider = current_provider.get()
            config = provider.resolve(Config)
            assert config.host == "localhost"

    def test_resets_context_var_after_exit(self):
        registry = ServiceRegistry()

        with registry.app_scope():
            pass

        assert current_provider.get(None) is None

    def test_service_provider_proxy(self):
        """The module-level `service_provider` proxy should delegate to the current scope."""
        registry = ServiceRegistry()
        config = Config(host="proxy-test", port=1234)
        registry.register_instance(config)

        with registry.app_scope():
            assert service_provider.resolve(Config) is config

    def test_nested_scopes(self):
        """Inner scope should shadow the outer one in the context var."""
        registry = ServiceRegistry()
        outer_config = Config(host="outer", port=1)
        inner_config = Config(host="inner", port=2)

        registry.register_instance(outer_config)

        with registry.app_scope() as outer_provider:
            assert outer_provider.resolve(Config).host == "outer"

            # Override the value in the same registry for the inner scope
            inner_registry = ServiceRegistry()
            inner_registry.register_instance(inner_config)

            inner_ctx = AppContext()
            inner_provider = DefaultServiceProvider(outer_provider, inner_registry, inner_ctx, AppContext)
            with inner_ctx.ctx, set_cvar(current_provider, inner_provider):
                assert inner_provider.resolve(Config).host == "inner"
                assert current_provider.get().resolve(Config).host == "inner"

            # After exiting inner scope, outer is restored
            assert current_provider.get().resolve(Config).host == "outer"


class TestCreateOnEnter:
    def test_eager_resolution(self):
        """Services with create_on_enter=True should be resolved when the scope is entered."""
        created = []

        class Pool:
            def __init__(self, config: Config):
                self.config = config
                created.append("pool")

        registry = ServiceRegistry()
        registry.register_instance(Config(host="localhost", port=5432))
        registry.register(Pool, create_on_enter=True)

        assert created == []
        with registry.app_scope() as provider:
            assert created == ["pool"]
            # Should return the same cached instance
            pool = provider.resolve(Pool)
            assert isinstance(pool, Pool)
            assert created == ["pool"]

    def test_eager_generator_factory(self):
        """Generator factories with create_on_enter=True should run setup on scope entry and cleanup on exit."""
        events: list[str] = []

        def create_pool(config: Config):
            events.append("setup")
            yield f"pool({config.host})"
            events.append("cleanup")

        registry = ServiceRegistry()
        registry.register_instance(Config(host="localhost", port=5432))
        registry.register(str, create_pool, create_on_enter=True)

        assert events == []
        with registry.app_scope() as provider:
            assert events == ["setup"]
            assert provider.resolve(str) == "pool(localhost)"
        assert events == ["setup", "cleanup"]

    def test_non_eager_not_resolved(self):
        """Services without create_on_enter should NOT be resolved on scope entry."""
        created = []

        class Pool:
            def __init__(self, config: Config):
                created.append("pool")

        registry = ServiceRegistry()
        registry.register_instance(Config(host="localhost", port=5432))
        registry.register(Pool)

        with registry.app_scope():
            assert created == []


class TestAppScopeLifecycle:
    def test_no_auto_close_on_scope_exit(self):
        """Services registered without a factory should NOT be auto-closed on scope exit."""
        closed = []

        class Pool:
            def __init__(self, config: Config):
                self.config = config

            def close(self):
                closed.append("pool")

        registry = ServiceRegistry()
        registry.register_instance(Config(host="localhost", port=5432))
        registry.register(Pool)

        with registry.app_scope() as provider:
            provider.resolve(Pool)

        assert closed == []

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
        registry.register_instance(Config(host="localhost", port=5432))
        registry.register(DBPool, db_pool_factory)

        with registry.app_scope() as provider:
            pool = provider.resolve(DBPool)
            assert pool.config.host == "localhost"
            assert not closed

        assert closed
