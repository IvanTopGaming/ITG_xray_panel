from typing import Iterator

from panel_core.xray.protocol import LOG_TAIL_LINES


class LocalXrayGateway:
    def has_local_xray(self) -> bool:
        return True

    def apply_config(self, validate: bool = True) -> None:
        from panel_core.xray import engine

        return engine.generate_config_file(validate=validate)

    def restart(self) -> None:
        from panel_core.xray import engine

        return engine.restart_xray_container()

    def add_user(self, inbound_tag: str, client_obj) -> bool:
        from panel_core.xray import grpc_client

        return grpc_client._api_add_user_grpc(inbound_tag, client_obj)

    def remove_user(self, inbound_tag: str, email: str) -> bool:
        from panel_core.xray import grpc_client

        return grpc_client._api_remove_user_grpc(inbound_tag, email)

    def stream_logs(self, tail_lines: int = LOG_TAIL_LINES) -> Iterator[str]:
        from panel_core.xray import engine

        return engine.stream_xray_logs(tail_lines)

    def update_geo(self) -> None:
        from panel_core.xray import engine

        return engine.update_geo_db()

    def reset_user_counters(self, inbound_tag: str, email: str, runtime_email: str) -> None:
        from panel_core.xray import grpc_client

        return grpc_client.reset_user_counters(inbound_tag, email, runtime_email)

    def reset_inbound_counters(self, inbound_tag: str) -> None:
        from panel_core.xray import grpc_client

        return grpc_client.reset_inbound_counters(inbound_tag)
