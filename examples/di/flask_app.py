import signal
from collections.abc import Generator
from dataclasses import dataclass

from cheroot.wsgi import Server
from flask import Flask, Request

from localpost.di._services import ServiceRegistry, service_provider
from localpost.di.flask import RequestContext, init_app, propagate_context


@dataclass
class Config:
    db_dsn: str


class DBConnectionPool:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def close(self):
        print(f"  Closing DB pool ({self.dsn})")


def create_db_pool(config: Config) -> Generator[DBConnectionPool]:
    pool = DBConnectionPool(config.db_dsn)
    print(f"  Created DB pool ({pool.dsn})")
    try:
        yield pool
    finally:
        pool.close()


class UserRepository:
    """Request-scoped: depends on the DB pool and the current Flask request."""

    def __init__(self, db: DBConnectionPool, req: Request):
        self.db = db
        self.request_path = req.path

    def get_current_user(self) -> str:
        return f"user (from {self.request_path}, db={self.db.dsn})"


services = ServiceRegistry()
services.register_value(Config(db_dsn=":memory:"))
services.register(DBConnectionPool, create_db_pool)
services.register(UserRepository, scope=RequestContext)

app = Flask(__name__)
init_app(app, services)


@app.get("/")
def index() -> str:
    repo = service_provider.resolve(UserRepository)
    return f"Hello, {repo.get_current_user()}!\n"


def main():
    with services.app_scope():
        # Wrap after entering app scope so worker threads inherit the context
        server = Server(("127.0.0.1", 8080), propagate_context(app))
        signal.signal(signal.SIGINT, lambda *_: server.stop())
        signal.signal(signal.SIGTERM, lambda *_: server.stop())
        print("Listening on http://127.0.0.1:8080")
        server.start()
    print("App scope closed, all resources cleaned up.")


if __name__ == "__main__":
    main()
