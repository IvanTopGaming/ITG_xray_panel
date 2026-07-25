from typing import Optional, Protocol, runtime_checkable

from panel_core.xray.protocol import LOG_TAIL_LINES


@runtime_checkable
class XrayGateway(Protocol):
    def apply_config(self, validate: bool = True) -> None: ...

    def restart(self) -> None: ...

    def add_user(self, inbound_tag: str, client_obj) -> bool: ...

    def remove_user(self, inbound_tag: str, email: str) -> bool: ...

    def stream_logs(self, tail_lines: int = LOG_TAIL_LINES): ...

    def update_geo(self): ...


class LocalXrayGateway:
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

    def stream_logs(self, tail_lines: int = LOG_TAIL_LINES):
        from panel_core.xray import engine

        return engine.stream_xray_logs(tail_lines)

    def update_geo(self):
        from panel_core.xray import engine

        return engine.update_geo_db()


_gateway = None


def set_xray_gateway(gateway: Optional[XrayGateway]) -> None:
    global _gateway
    _gateway = gateway


def get_xray_gateway() -> XrayGateway:
    global _gateway
    if _gateway is None:
        _gateway = LocalXrayGateway()
    return _gateway
