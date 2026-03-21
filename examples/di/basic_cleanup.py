from dataclasses import dataclass

from icecream import ic

from localpost.di._services import AppContext, ServiceProvider, ServiceRegistry


@dataclass
class Config:
    host: str
    port: int

    db_dsn: str


# ctx and sp are just to show how to use things
def create_db_conn_pool(conf: Config, ctx: AppContext, sp: ServiceProvider):
    db = DBConnectionPool(conf.db_dsn)
    try:
        yield db
    finally:
        db.close()


class DBConnectionPool:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def close(self):
        pass


class Server:
    def __init__(self, config: Config, db: DBConnectionPool):
        self.config = config
        self.db = db

    def close(self):
        pass


def main():
    services = ServiceRegistry()
    services.register_instance(Config(host="127.0.0.1", port=8080, db_dsn=":memory:"))
    services.register(DBConnectionPool, create_db_conn_pool)
    services.register(Server)

    with services.app_scope() as service_provider:
        server = service_provider.resolve(Server)
        ic(server, server.db)


if __name__ == "__main__":
    main()
