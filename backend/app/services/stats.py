import ipaddress
import os
import grpc
import re
import logging
from datetime import datetime, timedelta
import json
from sqlalchemy import text
from app.models import Inbound, Client, DomainStat, NodeClientTraffic, NotificationLog
from app.extensions import db, scheduler
from app.proxyman.command import command_pb2, command_pb2_grpc
from app.stats.command import (
    command_pb2 as stats_command_pb2,
    command_pb2_grpc as stats_command_pb2_grpc,
)
from common.protocol import user_pb2
from common.serial import typed_message_pb2
from proxy.vless import account_pb2
from app.services.xray import (
    generate_config_file,
    restart_xray_container,
    ACCESS_LOG_PATH,
)
from app.services.runtime_identity import build_runtime_email, parse_runtime_email

XRAY_API_HOST = os.getenv("XRAY_API_HOST", "xray-core:10085")
ACCESS_LOG_OFFSET_PATH = f"{ACCESS_LOG_PATH}.offset"
_grpc_channel = None
logger = logging.getLogger(__name__)

# Regex to extract client IP, destination host, and runtime email from access log.
# Group 1: client IP, Group 2: dest host (optional), Group 3: runtime email
_ACCEPT_FULL = re.compile(
    r"(\d{1,3}(?:\.\d{1,3}){3}):\d+\s+accepted\s+"
    r"(?:[a-zA-Z]+:)?([^\s:]+):\d+.*?email:\s+(\S+)"
)
_ACCEPT_BASIC = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3}):\d+\s+accepted\s+.*?email:\s+(\S+)")


def _is_ip_address(s: str) -> bool:
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def _ten_min_bucket(dt: datetime) -> int:
    """Return unix timestamp of the start of the current 10-minute window."""
    floored = dt.replace(minute=(dt.minute // 10) * 10, second=0, microsecond=0)
    return int(floored.timestamp())


def _upsert_snapshot(entity_type, entity_id, inbound_tag, bucket, up_delta, down_delta):
    """Upsert a traffic snapshot row, accumulating deltas."""
    if up_delta == 0 and down_delta == 0:
        return
    db.session.execute(
        text(
            """
            INSERT INTO traffic_snapshot
                (entity_type, entity_id, inbound_tag, bucket, up, down)
            VALUES
                (:et, :eid, :itag, :bucket, :up, :down)
            ON CONFLICT(entity_type, entity_id, inbound_tag, bucket) DO UPDATE SET
                up   = traffic_snapshot.up   + excluded.up,
                down = traffic_snapshot.down + excluded.down
            """
        ),
        {
            "et": entity_type,
            "eid": entity_id,
            "itag": inbound_tag or "",
            "bucket": bucket,
            "up": int(up_delta),
            "down": int(down_delta),
        },
    )


def _upsert_domain_stat(date_str, domain, client_email, inbound_tag, count):
    """Upsert a domain stat row, accumulating hit counts."""
    db.session.execute(
        text(
            """
            INSERT INTO domain_stat
                (date, domain, client_email, inbound_tag, hit_count)
            VALUES
                (:date, :domain, :email, :tag, :count)
            ON CONFLICT(date, domain, client_email, inbound_tag) DO UPDATE SET
                hit_count = domain_stat.hit_count + excluded.hit_count
            """
        ),
        {
            "date": date_str,
            "domain": domain,
            "email": client_email or "",
            "tag": inbound_tag or "",
            "count": int(count),
        },
    )


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
        logger.warning("gRPC add user failed for %s/%s: %s", inbound_tag, client_obj.email, e)
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
        logger.warning("gRPC remove user failed for %s/%s: %s", inbound_tag, email, e)
        return False


def sync_traffic_stats():
    inbounds = Inbound.query.all()
    clients = Client.query.filter_by(enable=True).all()
    if not inbounds:
        return
    try:
        channel = get_channel()
        stub = stats_command_pb2_grpc.StatsServiceStub(channel)
        has_updates = False
        now = datetime.now()
        bucket = _ten_min_bucket(now)

        for c in clients:
            runtime_email = build_runtime_email(c.inbound_tag, c.email)
            up_delta = 0
            down_delta = 0
            try:
                u = stub.QueryStats(
                    stats_command_pb2.QueryStatsRequest(
                        pattern=f"user>>>{runtime_email}>>>traffic>>>uplink", reset=True
                    ),
                    timeout=1,
                )
                if u.stat:
                    delta = u.stat[0].value
                    c.up += delta
                    up_delta += delta
                    has_updates = True
                d = stub.QueryStats(
                    stats_command_pb2.QueryStatsRequest(
                        pattern=f"user>>>{runtime_email}>>>traffic>>>downlink", reset=True
                    ),
                    timeout=1,
                )
                if d.stat:
                    delta = d.stat[0].value
                    c.down += delta
                    down_delta += delta
                    has_updates = True
            except grpc.RpcError:
                continue

            if up_delta > 0 or down_delta > 0:
                _upsert_snapshot("user", c.email, c.inbound_tag, bucket, up_delta, down_delta)

        for ib in inbounds:
            up_delta = 0
            down_delta = 0
            try:
                u = stub.QueryStats(
                    stats_command_pb2.QueryStatsRequest(pattern=f"inbound>>>{ib.tag}>>>traffic>>>uplink", reset=True),
                    timeout=1,
                )
                if u.stat:
                    delta = u.stat[0].value
                    ib.up += delta
                    up_delta += delta
                    has_updates = True
                d = stub.QueryStats(
                    stats_command_pb2.QueryStatsRequest(pattern=f"inbound>>>{ib.tag}>>>traffic>>>downlink", reset=True),
                    timeout=1,
                )
                if d.stat:
                    delta = d.stat[0].value
                    ib.down += delta
                    down_delta += delta
                    has_updates = True
            except grpc.RpcError:
                continue

            if up_delta > 0 or down_delta > 0:
                _upsert_snapshot("inbound", ib.tag, "", bucket, up_delta, down_delta)

        if has_updates:
            db.session.commit()
    except grpc.RpcError as e:
        _close_channel()
        logger.warning("Traffic sync failed: %s", e)


def _global_node_usage_map():
    """Return {email: total_bytes_across_all_nodes} from node_client_traffic."""
    rows = db.session.query(
        NodeClientTraffic.email,
        NodeClientTraffic.up,
        NodeClientTraffic.down,
    ).all()
    out = {}
    for email, up, down in rows:
        out[email] = out.get(email, 0) + int(up or 0) + int(down or 0)
    return out


def check_limits_and_reset():
    clients = Client.query.filter_by(enable=True).all()
    now_dt = datetime.now()
    now_ts = int(now_dt.timestamp() * 1000)
    current_day = now_dt.day
    config_changed = False
    restart_required = False
    node_usage = _global_node_usage_map()

    for c in clients:
        if c.reset_day > 0 and c.reset_day == current_day:
            last_reset_dt = None
            if c.last_reset_time and c.last_reset_time > 0:
                try:
                    last_reset_dt = datetime.fromtimestamp(c.last_reset_time / 1000)
                except (OSError, OverflowError, ValueError):
                    last_reset_dt = None

            already_reset_today = last_reset_dt is not None and last_reset_dt.date() == now_dt.date()
            if not already_reset_today:
                c.up = 0
                c.down = 0
                c.last_reset_time = now_ts
                config_changed = True
                # Clear stale traffic notifications so the next cycle re-fires
                # 80% / 95% / exhausted warnings. Time-based expiry kinds are
                # intentionally untouched — they belong to the lifecycle of
                # this Client.id, not its billing cycle.
                NotificationLog.query.filter(
                    NotificationLog.client_id == c.id,
                    NotificationLog.kind.in_(("traffic_80", "traffic_95", "traffic_exhausted")),
                ).delete(synchronize_session=False)
                try:
                    channel = get_channel()
                    stub = stats_command_pb2_grpc.StatsServiceStub(channel)
                    stub.QueryStats(
                        stats_command_pb2.QueryStatsRequest(
                            pattern=f"user>>>{build_runtime_email(c.inbound_tag, c.email)}>>>traffic>>>uplink",
                            reset=True,
                        )
                    )
                    stub.QueryStats(
                        stats_command_pb2.QueryStatsRequest(
                            pattern=f"user>>>{build_runtime_email(c.inbound_tag, c.email)}>>>traffic>>>downlink",
                            reset=True,
                        )
                    )
                except grpc.RpcError as e:
                    logger.debug("Failed to reset gRPC counters for %s: %s", c.email, e)

        global_used = (c.up + c.down) + int(node_usage.get(c.email, 0))
        global_over = (c.global_limit_bytes or 0) > 0 and global_used >= (c.global_limit_bytes or 0)
        per_node_over = c.limit_bytes > 0 and (c.up + c.down) >= c.limit_bytes
        if (c.expiry_time > 0 and now_ts > c.expiry_time) or per_node_over or global_over:
            c.enable = False
            config_changed = True
            try:
                ib = Inbound.query.filter_by(tag=c.inbound_tag).first()
                if ib and ib.protocol in ["vless", "vmess"]:
                    removed = _api_remove_user_grpc(c.inbound_tag, c.email)
                    if not removed:
                        restart_required = True
                else:
                    restart_required = True
            except Exception as e:
                restart_required = True
                logger.warning(
                    "Failed to process limit disable for %s/%s: %s",
                    c.inbound_tag,
                    c.email,
                    e,
                )

    if config_changed:
        db.session.commit()
        generate_config_file()
        if restart_required:
            restart_xray_container()


def _read_access_offset():
    try:
        with open(ACCESS_LOG_OFFSET_PATH, "r", encoding="utf-8") as file_obj:
            return max(0, int(file_obj.read().strip()))
    except (OSError, ValueError):
        return 0


def _write_access_offset(offset):
    try:
        with open(ACCESS_LOG_OFFSET_PATH, "w", encoding="utf-8") as file_obj:
            file_obj.write(str(max(0, int(offset))))
    except (OSError, ValueError):
        logger.debug("Failed to write access log offset", exc_info=True)


def _parse_access_logs_logic():
    if not os.path.exists(ACCESS_LOG_PATH):
        return

    try:
        file_size = os.path.getsize(ACCESS_LOG_PATH)
        offset = _read_access_offset()
        if offset > file_size:
            offset = 0

        with open(ACCESS_LOG_PATH, "r", encoding="utf-8", errors="replace") as file_obj:
            file_obj.seek(offset)
            logs = file_obj.read()
            new_offset = file_obj.tell()

        _write_access_offset(new_offset)

        if not logs:
            return

        ip_updates: dict[str, set] = {}  # runtime_email → {ips}
        domain_updates: dict[str, dict] = {}  # runtime_email → {domain: count}

        for line in logs.split("\n"):
            # Try full pattern with destination host
            match = _ACCEPT_FULL.search(line)
            if match:
                ip = match.group(1)
                dest_host = match.group(2)
                runtime_email = match.group(3)
            else:
                # Fallback: no destination captured
                match = _ACCEPT_BASIC.search(line)
                if not match:
                    continue
                ip = match.group(1)
                dest_host = None
                runtime_email = match.group(2)

            if runtime_email not in ip_updates:
                ip_updates[runtime_email] = set()
            ip_updates[runtime_email].add(ip)

            if dest_host and not _is_ip_address(dest_host):
                if runtime_email not in domain_updates:
                    domain_updates[runtime_email] = {}
                domain_updates[runtime_email][dest_host] = domain_updates[runtime_email].get(dest_host, 0) + 1

        if not ip_updates and not domain_updates:
            return

        now_ts = int(datetime.now().timestamp() * 1000)
        today_str = datetime.now().date().isoformat()
        all_emails = set(ip_updates) | set(domain_updates)

        for runtime_email in all_emails:
            inbound_tag, email = parse_runtime_email(runtime_email)
            if inbound_tag:
                matched_clients = Client.query.filter_by(inbound_tag=inbound_tag, email=email).all()
            else:
                matched_clients = Client.query.filter_by(email=email).all()

            for c in matched_clients:
                # Update last_seen and source_ips
                if runtime_email in ip_updates:
                    c.last_seen = now_ts
                    try:
                        current = json.loads(c.source_ips) if c.source_ips else []
                    except (TypeError, ValueError, json.JSONDecodeError):
                        current = []
                    for ip in ip_updates[runtime_email]:
                        if ip not in current:
                            current.insert(0, ip)
                    c.source_ips = json.dumps(current[:10])

                # Save domain stats
                if runtime_email in domain_updates:
                    for domain, count in domain_updates[runtime_email].items():
                        _upsert_domain_stat(today_str, domain, c.email, c.inbound_tag, count)

        db.session.commit()
    except Exception as e:
        logger.warning("Log parsing error: %s", e)


def cleanup_old_domain_stats():
    """Delete domain stats older than 90 days to prevent unbounded growth."""
    try:
        cutoff = (datetime.now() - timedelta(days=90)).date().isoformat()
        deleted = DomainStat.query.filter(DomainStat.date < cutoff).delete()
        if deleted:
            db.session.commit()
            logger.info("Cleaned up %d old domain stat rows", deleted)
    except Exception as e:
        logger.warning("Domain stat cleanup failed: %s", e)


def sync_traffic_job():
    with scheduler.app.app_context():
        sync_traffic_stats()


def check_limits_job():
    with scheduler.app.app_context():
        check_limits_and_reset()


def parse_access_logs():
    with scheduler.app.app_context():
        _parse_access_logs_logic()


def cleanup_stats_job():
    with scheduler.app.app_context():
        cleanup_old_domain_stats()


def reset_user_traffic(tag, email):
    client = Client.query.filter_by(inbound_tag=tag, email=email).first()
    if not client:
        raise Exception("User not found")
    runtime_email = build_runtime_email(tag, email)
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
    client.up = 0
    client.down = 0
    db.session.commit()


def reset_inbound_traffic(tag):
    ib = Inbound.query.filter_by(tag=tag).first()
    if not ib:
        raise Exception("Inbound not found")
    for client in ib.clients:
        reset_user_traffic(tag, client.email)
    try:
        channel = get_channel()
        stub = stats_command_pb2_grpc.StatsServiceStub(channel)
        stub.QueryStats(stats_command_pb2.QueryStatsRequest(pattern=f"inbound>>>{tag}>>>traffic>>>uplink", reset=True))
        stub.QueryStats(
            stats_command_pb2.QueryStatsRequest(pattern=f"inbound>>>{tag}>>>traffic>>>downlink", reset=True)
        )
    except grpc.RpcError as e:
        logger.debug("Failed to reset inbound traffic counters for %s: %s", tag, e)
    ib.up = 0
    ib.down = 0
    db.session.commit()


def bulk_delete_users(users_list):
    if not users_list:
        return 0

    grpc_removals = []
    restart_required = False
    deleted_count = 0

    for user in users_list:
        tag = user.get("tag")
        email = user.get("email")
        if not tag or not email:
            continue

        client = Client.query.filter_by(inbound_tag=tag, email=email).first()
        if not client:
            continue

        ib = Inbound.query.filter_by(tag=tag).first()
        was_enabled = bool(client.enable)
        db.session.delete(client)
        deleted_count += 1

        if ib and ib.protocol in ["vless", "vmess"] and was_enabled:
            grpc_removals.append((tag, email))
        else:
            restart_required = True

    if deleted_count == 0:
        return 0

    db.session.commit()
    generate_config_file()

    if restart_required:
        restart_xray_container()
    else:
        grpc_failed = False
        for tag, email in grpc_removals:
            if not _api_remove_user_grpc(tag, email):
                grpc_failed = True
        if grpc_failed:
            restart_xray_container()

    return deleted_count


def bulk_reset_traffic(users_list):
    for u in users_list:
        reset_user_traffic(u["tag"], u["email"])
