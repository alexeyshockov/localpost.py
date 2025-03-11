from textwrap import dedent

from testcontainers.kafka import RedpandaContainer as BaseRedpandaContainer


# See:
#  - https://github.com/abiosoft/colima/issues/71#issuecomment-1985144767
#  - https://stackoverflow.com/q/78149093/322079
class RedpandaContainer(BaseRedpandaContainer):
    def get_container_host_ip(self) -> str:
        ip = super().get_container_host_ip()
        if ip == "localhost":
            # At least on macOS (with Lima/Colima), localhost resolves to IPv6 by default, which causes issues
            # (see https://github.com/testcontainers/testcontainers-python/issues/553 and others)
            return "127.0.0.1"
        return ip

    def tc_start(self) -> None:
        internal = f"internal://{self.get_docker_client().bridge_ip(self.get_wrapped_container().id)}:29092"
        external = f"external://{self.get_container_host_ip()}:{self.get_exposed_port(self.redpanda_port)}"
        data = dedent(
            f"""
            #!/bin/bash
            rpk redpanda start --mode dev-container --smp 1 \
            --kafka-addr internal://0.0.0.0:29092,external://0.0.0.0:{self.redpanda_port} \
            --advertise-kafka-addr {internal},{external} \
            --schema-registry-addr internal://0.0.0.0:28081,external://0.0.0.0:{self.schema_registry_port}
            """
        ).strip().encode("utf-8")

        self.create_file(data, RedpandaContainer.TC_START_SCRIPT)
