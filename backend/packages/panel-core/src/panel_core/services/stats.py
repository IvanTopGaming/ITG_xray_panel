import calendar
import ipaddress
import os
import re
import logging
import threading
from datetime import datetime, timedelta
import json
from sqlalchemy import text
from panel_core.models import Inbound, Client, DomainStat, NotificationLog
from panel_core.extensions import db, scheduler
from panel_core.xray.grpc_client import (
    grpc,
    stats_command_pb2,
    stats_command_pb2_grpc,
    get_channel,
    _close_channel,
)
from panel_core.xray import (
    generate_config_file,
    has_local_xray,
    restart_xray_container,
    _api_add_user_grpc,  # noqa: F401 — re-exported for consumers importing it from this module
    _api_remove_user_grpc,
)
from panel_core.xray.engine import ACCESS_LOG_PATH
from panel_core.services.runtime_identity import build_runtime_email, parse_runtime_email

ACCESS_LOG_OFFSET_PATH = f"{ACCESS_LOG_PATH}.offset"
logger = logging.getLogger(__name__)


_RESET_LOCK = threading.Lock()


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

    floored = dt.replace(minute=(dt.minute // 10) * 10, second=0, microsecond=0)
    return int(floored.timestamp())


def _upsert_snapshot(entity_type, entity_id, inbound_tag, bucket, up_delta, down_delta):

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


def _upsert_node_snapshot(panel_id, entity_type, entity_id, inbound_tag, bucket, up_delta, down_delta):

    if up_delta == 0 and down_delta == 0:
        return
    db.session.execute(
        text(
            """
            INSERT INTO node_traffic_snapshot
                (panel_id, entity_type, entity_id, inbound_tag, bucket, up, down)
            VALUES
                (:pid, :et, :eid, :itag, :bucket, :up, :down)
            ON CONFLICT(panel_id, entity_type, entity_id, inbound_tag, bucket) DO UPDATE SET
                up   = node_traffic_snapshot.up   + excluded.up,
                down = node_traffic_snapshot.down + excluded.down
            """
        ),
        {
            "pid": int(panel_id),
            "et": entity_type,
            "eid": entity_id,
            "itag": inbound_tag or "",
            "bucket": bucket,
            "up": int(up_delta),
            "down": int(down_delta),
        },
    )


def _upsert_domain_stat(date_str, domain, client_email, inbound_tag, count):

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


def sync_traffic_stats():

    inbounds = Inbound.query.all()
    clients = Client.query.filter_by(enable=True).all()
    if not inbounds:
        return

    try:
        channel = get_channel()
        stub = stats_command_pb2_grpc.StatsServiceStub(channel)
    except grpc.RpcError as e:
        _close_channel()
        logger.info("Traffic sync failed (channel init): %s", e)
        return

    def _query_pair(pattern_up: str, pattern_down: str) -> tuple[int, int] | None:
        try:
            with _RESET_LOCK:
                u = stub.QueryStats(stats_command_pb2.QueryStatsRequest(pattern=pattern_up, reset=True), timeout=1)
                d = stub.QueryStats(stats_command_pb2.QueryStatsRequest(pattern=pattern_down, reset=True), timeout=1)
        except grpc.RpcError:
            return None
        up_delta = u.stat[0].value if u.stat else 0
        down_delta = d.stat[0].value if d.stat else 0
        return up_delta, down_delta

    user_deltas: list[tuple[Client, int, int]] = []
    inbound_deltas: list[tuple[Inbound, int, int]] = []

    for c in clients:
        runtime_email = build_runtime_email(c.inbound_tag, c.email)
        pair = _query_pair(
            f"user>>>{runtime_email}>>>traffic>>>uplink",
            f"user>>>{runtime_email}>>>traffic>>>downlink",
        )
        if pair is None:
            continue
        up_d, down_d = pair
        if up_d or down_d:
            user_deltas.append((c, up_d, down_d))

    for ib in inbounds:
        pair = _query_pair(
            f"inbound>>>{ib.tag}>>>traffic>>>uplink",
            f"inbound>>>{ib.tag}>>>traffic>>>downlink",
        )
        if pair is None:
            continue
        up_d, down_d = pair
        if up_d or down_d:
            inbound_deltas.append((ib, up_d, down_d))

    if not user_deltas and not inbound_deltas:
        return

    bucket = _ten_min_bucket(datetime.now())
    for c, up_d, down_d in user_deltas:
        c.up += up_d
        c.down += down_d
        _upsert_snapshot("user", c.email, c.inbound_tag, bucket, up_d, down_d)
    for ib, up_d, down_d in inbound_deltas:
        ib.up += up_d
        ib.down += down_d
        _upsert_snapshot("inbound", ib.tag, "", bucket, up_d, down_d)
    db.session.commit()

    try:
        from panel_core.services.notifications import emit_if_new, evaluate_traffic

        for c, _up_d, _down_d in user_deltas:
            if c.telegram_id is None:
                continue
            kind = evaluate_traffic(c)
            if kind is None:
                continue
            used_bytes = (c.up or 0) + (c.down or 0)
            emit_if_new(
                "traffic_notification",
                kind,
                c,
                {
                    "used_bytes": used_bytes,
                    "limit_bytes": c.limit_bytes,
                    "limit_kind": "per_inbound",
                    "pct": round(used_bytes / c.limit_bytes, 4),
                },
            )
    except Exception as e:
        logger.warning("traffic notification pass failed: %s", e)


def check_limits_and_reset():

    clients = Client.query.filter_by(enable=True).all()
    now_dt = datetime.now()
    now_ts = int(now_dt.timestamp() * 1000)
    current_day = now_dt.day

    to_reset: list[Client] = []
    to_disable: list[tuple[Client, str]] = []

    days_in_month = calendar.monthrange(now_dt.year, now_dt.month)[1]

    for c in clients:
        effective_reset_day = min(c.reset_day, days_in_month) if c.reset_day > 0 else 0
        if effective_reset_day > 0 and effective_reset_day == current_day:
            last_reset_dt = None
            if c.last_reset_time and c.last_reset_time > 0:
                try:
                    last_reset_dt = datetime.fromtimestamp(c.last_reset_time / 1000)
                except (OSError, OverflowError, ValueError):
                    last_reset_dt = None
            already_reset_today = last_reset_dt is not None and last_reset_dt.date() == now_dt.date()
            if not already_reset_today:
                to_reset.append(c)

        over_limit = c.limit_bytes > 0 and (c.up + c.down) >= c.limit_bytes
        expired = c.expiry_time > 0 and now_ts > c.expiry_time
        if expired or over_limit:
            to_disable.append((c, "over_limit" if over_limit else "expired"))

    try:
        from panel_core.services.notifications import emit_if_new, evaluate_expiry

        for c in clients:
            if c.telegram_id is None:
                continue
            kind = evaluate_expiry(c, now_ts)
            if kind is None:
                continue
            emit_if_new(
                "expiry_notification",
                kind,
                c,
                {"expiry_time_ms": c.expiry_time},
            )
    except Exception as e:
        logger.warning("expiry notification pass failed: %s", e)

    if not to_reset and not to_disable:
        return

    relevant_tags = {c.inbound_tag for c in to_reset} | {c.inbound_tag for c, _ in to_disable}
    inbounds_by_tag = (
        {ib.tag: ib for ib in Inbound.query.filter(Inbound.tag.in_(relevant_tags)).all()} if relevant_tags else {}
    )

    if to_reset:
        try:
            channel = get_channel()
            stub = stats_command_pb2_grpc.StatsServiceStub(channel)
            for c in to_reset:
                runtime_email = build_runtime_email(c.inbound_tag, c.email)
                for suffix in ("uplink", "downlink"):
                    try:
                        with _RESET_LOCK:
                            stub.QueryStats(
                                stats_command_pb2.QueryStatsRequest(
                                    pattern=f"user>>>{runtime_email}>>>traffic>>>{suffix}",
                                    reset=True,
                                )
                            )
                    except grpc.RpcError as e:
                        logger.debug("Failed to reset gRPC %s for %s: %s", suffix, c.email, e)
        except grpc.RpcError as e:
            logger.debug("Failed to acquire gRPC channel for monthly reset: %s", e)

    restart_required = False
    for c, _reason in to_disable:
        ib = inbounds_by_tag.get(c.inbound_tag)
        try:
            if ib and ib.protocol in ("vless", "vmess"):
                if not _api_remove_user_grpc(c.inbound_tag, c.email):
                    restart_required = True
            else:
                restart_required = True
        except Exception as e:
            restart_required = True
            logger.warning("Failed to process limit disable for %s/%s: %s", c.inbound_tag, c.email, e)

    for c in to_reset:
        c.up = 0
        c.down = 0
        c.last_reset_time = now_ts

        NotificationLog.query.filter(
            NotificationLog.client_id == c.id,
            NotificationLog.kind.in_(("traffic_80", "traffic_95", "traffic_exhausted")),
        ).delete(synchronize_session=False)

    for c, reason in to_disable:
        c.enable = False
        logger.info("disabled %s/%s: %s", c.inbound_tag, c.email, reason)

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

        ip_updates: dict[str, set] = {}
        domain_updates: dict[str, dict] = {}

        for line in logs.split("\n"):
            match = _ACCEPT_FULL.search(line)
            if match:
                ip = match.group(1)
                dest_host = match.group(2)
                runtime_email = match.group(3)
            else:
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

                if runtime_email in domain_updates:
                    for domain, count in domain_updates[runtime_email].items():
                        _upsert_domain_stat(today_str, domain, c.email, c.inbound_tag, count)

        db.session.commit()
    except Exception as e:
        logger.info("Log parsing error: %s", e)


def cleanup_old_domain_stats():

    try:
        cutoff = (datetime.now() - timedelta(days=90)).date().isoformat()
        deleted = DomainStat.query.filter(DomainStat.date < cutoff).delete()
        if deleted:
            db.session.commit()
            logger.info("Cleaned up %d old domain stat rows", deleted)
    except Exception as e:
        logger.info("Domain stat cleanup failed: %s", e)


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
    if has_local_xray():
        _reset_user_counters_in_xray(tag, email, runtime_email)
    client.up = 0
    client.down = 0
    db.session.commit()


def _reset_user_counters_in_xray(tag, email, runtime_email):
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


def reset_inbound_traffic(tag):
    ib = Inbound.query.filter_by(tag=tag).first()
    if not ib:
        raise Exception("Inbound not found")
    for client in ib.clients:
        reset_user_traffic(tag, client.email)
    if has_local_xray():
        _reset_inbound_counters_in_xray(tag)
    ib.up = 0
    ib.down = 0
    db.session.commit()


def _reset_inbound_counters_in_xray(tag):
    try:
        channel = get_channel()
        stub = stats_command_pb2_grpc.StatsServiceStub(channel)
        stub.QueryStats(stats_command_pb2.QueryStatsRequest(pattern=f"inbound>>>{tag}>>>traffic>>>uplink", reset=True))
        stub.QueryStats(
            stats_command_pb2.QueryStatsRequest(pattern=f"inbound>>>{tag}>>>traffic>>>downlink", reset=True)
        )
    except grpc.RpcError as e:
        logger.debug("Failed to reset inbound traffic counters for %s: %s", tag, e)


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
