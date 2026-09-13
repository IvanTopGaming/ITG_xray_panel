import os
import grpc
import logging
from app.proxyman.command import command_pb2, command_pb2_grpc
from app.stats.command import (
    command_pb2 as stats_command_pb2,  # noqa: F401 — re-exported for the traffic collector
    command_pb2_grpc as stats_command_pb2_grpc,  # noqa: F401 — re-exported for the traffic collector
)
from common.protocol import user_pb2
from common.serial import typed_message_pb2
from proxy.vless import account_pb2
from panel_core.models import Inbound
from panel_core.services.runtime_identity import build_runtime_email

XRAY_API_HOST = os.getenv("XRAY_API_HOST", "xray-core:10085")
_grpc_channel = None
logger = logging.getLogger(__name__)


def _close_channel():
    global _grpc_channel
    if _grpc_channel is not None:
        try:
            _grpc_channel.close()
        except Exception:
            pass
        _grpc_channel = None


def get_channel():
    global _grpc_channel
    if _grpc_channel is None:
        _grpc_channel = grpc.insecure_channel(
            XRAY_API_HOST,
            options=[
                ("grpc.keepalive_time_ms", 10000),
                ("grpc.keepalive_timeout_ms", 5000),
                ("grpc.keepalive_permit_without_calls", 1),
            ],
        )
    return _grpc_channel


def _api_add_user_grpc(inbound_tag, client_obj):
    try:
        inbound = Inbound.query.filter_by(tag=inbound_tag).first()
        if inbound is None or inbound.protocol not in {"vless", "vmess"}:
            return False
        if inbound.protocol == "vmess":
            from proxy.vmess import account_pb2 as vmess_account_pb2

            account = vmess_account_pb2.Account(id=client_obj.id)
        else:
            account = account_pb2.Account(id=client_obj.id, flow=client_obj.flow or "", encryption="none")
        typed_acc = typed_message_pb2.TypedMessage(type=account.DESCRIPTOR.full_name, value=account.SerializeToString())
        user = user_pb2.User(
            level=0,
            email=build_runtime_email(inbound_tag, client_obj.email),
            account=typed_acc,
        )
        stub = command_pb2_grpc.HandlerServiceStub(get_channel())
        stub.AlterInbound(
            command_pb2.AlterInboundRequest(
                tag=inbound_tag,
                operation=typed_message_pb2.TypedMessage(
                    type=command_pb2.AddUserOperation(user=user).DESCRIPTOR.full_name,
                    value=command_pb2.AddUserOperation(user=user).SerializeToString(),
                ),
            ),
            timeout=3,
        )
        return True
    except grpc.RpcError as e:
        err_msg = str(e).lower()
        if "already exists" in err_msg:
            logger.info(
                "gRPC account requires reconciliation for %s/%s (already exists)", inbound_tag, client_obj.email
            )
            return False
        logger.info("gRPC add user failed for %s/%s: %s", inbound_tag, client_obj.email, e)
        return False


def _api_remove_user_grpc(inbound_tag, email):
    try:
        runtime_email = build_runtime_email(inbound_tag, email)
        stub = command_pb2_grpc.HandlerServiceStub(get_channel())
        stub.AlterInbound(
            command_pb2.AlterInboundRequest(
                tag=inbound_tag,
                operation=typed_message_pb2.TypedMessage(
                    type=command_pb2.RemoveUserOperation(email=runtime_email).DESCRIPTOR.full_name,
                    value=command_pb2.RemoveUserOperation(email=runtime_email).SerializeToString(),
                ),
            ),
            timeout=3,
        )
        return True
    except grpc.RpcError as e:
        err_msg = str(e).lower()
        if "not found" in err_msg:
            logger.info("gRPC remove user skipped for %s/%s (not found)", inbound_tag, email)
            return True
        logger.info("gRPC remove user failed for %s/%s: %s", inbound_tag, email, e)
        return False


def reset_user_counters(tag, email, runtime_email):
    try:
        channel = get_channel()
        stub = stats_command_pb2_grpc.StatsServiceStub(channel)
        stub.QueryStats(
            stats_command_pb2.QueryStatsRequest(pattern=f"user>>>{runtime_email}>>>traffic>>>uplink", reset=True)
        )
        stub.QueryStats(
            stats_command_pb2.QueryStatsRequest(pattern=f"user>>>{runtime_email}>>>traffic>>>downlink", reset=True)
        )
    except grpc.RpcError as e:
        logger.debug("Failed to reset user traffic counters for %s/%s: %s", tag, email, e)


def reset_inbound_counters(tag):
    try:
        channel = get_channel()
        stub = stats_command_pb2_grpc.StatsServiceStub(channel)
        stub.QueryStats(stats_command_pb2.QueryStatsRequest(pattern=f"inbound>>>{tag}>>>traffic>>>uplink", reset=True))
        stub.QueryStats(
            stats_command_pb2.QueryStatsRequest(pattern=f"inbound>>>{tag}>>>traffic>>>downlink", reset=True)
        )
    except grpc.RpcError as e:
        logger.debug("Failed to reset inbound traffic counters for %s: %s", tag, e)


def _runtime_epoch():
    import docker

    client = docker.from_env(timeout=2)
    try:
        container = client.containers.get("xray-core")
        state = container.attrs.get("State", {})
        epoch = state.get("StartedAt")
        if not state.get("Running") or not epoch or epoch.startswith("0001-"):
            raise RuntimeError("Xray runtime epoch is unavailable")
        return epoch
    finally:
        client.close()


def read_traffic_counters(pattern=""):
    epoch = _runtime_epoch()
    stub = stats_command_pb2_grpc.StatsServiceStub(get_channel())
    response = stub.QueryStats(stats_command_pb2.QueryStatsRequest(pattern=pattern, reset=False), timeout=3)
    if _runtime_epoch() != epoch:
        raise RuntimeError("Xray restarted during traffic collection; retry the sample")
    values = {}
    for stat in response.stat:
        if stat.value < 0:
            raise RuntimeError("Xray returned a negative traffic counter")
        values[stat.name] = int(stat.value)
    return epoch, values
