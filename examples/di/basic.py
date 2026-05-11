from dataclasses import dataclass

from icecream import ic

from localpost.di._services import ServiceRegistry


@dataclass
class Config:
    host: str
    port: int


class Server:
    def __init__(self, config: Config):
        self.config = config


def main():
    services = ServiceRegistry()
    services.register_instance(Config(host="127.0.0.1", port=8080))
    services.register(Server)

    with services.app_scope() as service_provider:
        server = service_provider.resolve(Server)
        ic(server)


if __name__ == "__main__":
    main()
