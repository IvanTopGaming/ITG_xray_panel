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

    def reset_user_counters(self, inbound_tag: str, email: str, runtime_email: str) -> None: ...

    def reset_inbound_counters(self, inbound_tag: str) -> None: ...


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

    def reset_user_counters(self, inbound_tag: str, email: str, runtime_email: str) -> None:
        return None

    def reset_inbound_counters(self, inbound_tag: str) -> None:
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

    def reset_user_counters(self, inbound_tag: str, email: str, runtime_email: str) -> None:
        return None

    def reset_inbound_counters(self, inbound_tag: str) -> None:
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
        raise RuntimeError(
            "no XrayGateway bound - every role factory binds one in create_app(); "
            "a test must bind one explicitly with set_xray_gateway()"
        )
    return _gateway
