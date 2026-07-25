from typing import Iterator, Optional, Protocol, runtime_checkable

from panel_core.xray.protocol import LOG_TAIL_LINES


@runtime_checkable
class XrayGateway(Protocol):
    def has_local_xray(self) -> bool: ...

    def apply_config(self, validate: bool = True) -> None: ...

    def restart(self) -> None: ...

    def add_user(self, inbound_tag: str, client_obj) -> bool: ...

    def remove_user(self, inbound_tag: str, email: str) -> bool: ...

    def stream_logs(self, tail_lines: int = LOG_TAIL_LINES) -> Iterator[str]: ...

    def update_geo(self) -> None: ...


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


class NullXrayGateway:
    def has_local_xray(self) -> bool:
        return False

    def apply_config(self, validate: bool = True) -> None:
        return None

    def restart(self) -> None:
        return None

    def add_user(self, inbound_tag: str, client_obj) -> bool:
        return True

    def remove_user(self, inbound_tag: str, email: str) -> bool:
        return True

    def stream_logs(self, tail_lines: int = LOG_TAIL_LINES) -> Iterator[str]:
        return iter(())

    def update_geo(self) -> None:
        return None


class LocalXrayUnavailable(RuntimeError):
    pass


class RemoteXrayGateway:
    def has_local_xray(self) -> bool:
        return False

    def apply_config(self, validate: bool = True) -> None:
        return None

    def restart(self) -> None:
        return None

    def add_user(self, inbound_tag: str, client_obj) -> bool:
        raise LocalXrayUnavailable(_unavailable_message("add_user"))

    def remove_user(self, inbound_tag: str, email: str) -> bool:
        raise LocalXrayUnavailable(_unavailable_message("remove_user"))

    def stream_logs(self, tail_lines: int = LOG_TAIL_LINES) -> Iterator[str]:
        raise LocalXrayUnavailable(_unavailable_message("stream_logs"))

    def update_geo(self) -> None:
        return None


def _unavailable_message(operation: str) -> str:
    return (
        f"{operation} requires a local Xray instance, which this role does not run. "
        f"Route the operation to a node by supplying a panel_id."
    )


_gateway = None


def set_xray_gateway(gateway: Optional[XrayGateway]) -> None:
    global _gateway
    _gateway = gateway


def xray_gateway_configured() -> bool:
    return _gateway is not None


def get_xray_gateway() -> XrayGateway:
    if _gateway is None:
        return LocalXrayGateway()
    return _gateway
