import calendar
import ipaddress
import json
import logging
import os
import re
from datetime import datetime

from panel_core.extensions import db, scheduler
from panel_core.models import AccessEntitlement, Client, Inbound, LogCheckpoint
from panel_core.services.reality_health import record_failures
from panel_core.services.entitlements import apply_client_activation_changes, refresh_client_entitlements
from panel_core.services.runtime_apply import (
    mark_runtime_dirty,
    prepare_runtime_config,
    runtime_lock,
    synchronize_runtime,
)
from panel_core.services.runtime_identity import parse_runtime_email
from panel_core.services.traffic_store import (
    _upsert_domain_stat,
    can_reenable,
    read_traffic_sample,
    settle_client_traffic,
    settle_inbound_traffic,
    start_traffic_cycle,
)
from panel_core.services.traffic_store import (
    bulk_delete_users as bulk_delete_users,
    cleanup_old_domain_stats as cleanup_old_domain_stats,
    cleanup_stats_job as cleanup_stats_job,
    reset_inbound_traffic as reset_inbound_traffic,
    reset_user_traffic as reset_user_traffic,
    _ten_min_bucket as _ten_min_bucket,
    _upsert_snapshot as _upsert_snapshot,
)
from panel_core.xray.engine import ACCESS_LOG_PATH, ERROR_LOG_PATH
from panel_core.xray.grpc_client import get_channel as get_channel, stats_command_pb2_grpc as stats_command_pb2_grpc

ACCESS_LOG_OFFSET_PATH = f"{ACCESS_LOG_PATH}.offset"
REALITY_OFFSET_PATH = f"{ERROR_LOG_PATH}.reality.offset"
_REALITY_REFUSED = "REALITY: processed invalid connection"
logger = logging.getLogger(__name__)
_ACCEPT_FULL = re.compile(
    r"(\[[^\]]+\]|\d{1,3}(?:\.\d{1,3}){3}):\d+\s+accepted\s+(?:[a-zA-Z]+:)?(\[[^\]]+\]|[^\s:]+):\d+.*?email:\s+(\S+)"
)
_ACCEPT_BASIC = re.compile(r"(\[[^\]]+\]|\d{1,3}(?:\.\d{1,3}){3}):\d+\s+accepted\s+.*?email:\s+(\S+)")


def _is_ip_address(value):
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def sync_traffic_stats():
    with runtime_lock():
        db.session.expire_all()
        if not Inbound.query.first():
            return
        sample = read_traffic_sample()
        changed = []
        for client in Client.query.all():
            if client.up is None or client.down is None or client.up < 0 or client.down < 0:
                logger.error("Invalid stored traffic for %s/%s", client.inbound_tag, client.email)
                continue
            if any(settle_client_traffic(client, sample=sample)):
                changed.append(client.id)
        for inbound in Inbound.query.all():
            settle_inbound_traffic(inbound, sample=sample)
        db.session.commit()
    try:
        from panel_core.services.notifications import emit_if_new, evaluate_traffic

        for client_id in changed:
            client = db.session.get(Client, client_id, populate_existing=True)
            if client is None or client.telegram_id is None:
                continue
            kind = evaluate_traffic(client)
            if kind is not None:
                used = client.up + client.down
                emit_if_new(
                    "traffic_notification",
                    kind,
                    client,
                    {
                        "used_bytes": used,
                        "limit_bytes": client.limit_bytes,
                        "limit_kind": "per_inbound",
                        "pct": round(used / client.limit_bytes, 4),
                    },
                )
    except Exception:
        logger.exception("Traffic notification pass failed")


def _reset_due(client, now):
    if not client.reset_day:
        return False
    boundary = now.replace(
        day=min(client.reset_day, calendar.monthrange(now.year, now.month)[1]),
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    if boundary > now:
        year, month = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
        boundary = boundary.replace(day=1, year=year, month=month).replace(
            day=min(client.reset_day, calendar.monthrange(year, month)[1])
        )
    return (client.last_reset_time or 0) < int(boundary.timestamp() * 1000)


def check_limits_and_reset():
    with runtime_lock():
        db.session.expire_all()
        now = datetime.now()
        now_ms = int(now.timestamp() * 1000)
        clients = Client.query.all()
        activation_changes = [(client, bool(client.enable)) for client in clients]
        notify_ids = [client.id for client in clients if client.telegram_id is not None]
        changed = False
        sample = None
        expired_source_clients = {
            source.client_id
            for source in AccessEntitlement.query.join(
                Client,
                (Client.id == AccessEntitlement.client_id)
                & (Client.active_entitlement_source == AccessEntitlement.source_id),
            ).filter(
                Client.provisioning_key.isnot(None),
                AccessEntitlement.expires_at_ms > 0,
                AccessEntitlement.expires_at_ms <= now_ms,
            )
        }
        for client in clients:
            invalid = (
                any(
                    value is None or not isinstance(value, int) or value < 0
                    for value in (client.up, client.down, client.limit_bytes, client.expiry_time, client.reset_day)
                )
                or client.reset_day > 31
            )
            if invalid:
                if client.enable:
                    client.enable, client.disable_reason = False, "invalid"
                    changed = True
                logger.error("Invalid limits for %s/%s; client isolated", client.inbound_tag, client.email)
                continue
            if client.id in expired_source_clients:
                if sample is None:
                    sample = read_traffic_sample()
                was_enabled = bool(client.enable)
                refresh_client_entitlements(client, sample=sample, now_ms=now_ms)
                changed = changed or was_enabled != bool(client.enable)
            if _reset_due(client, now):
                if sample is None:
                    sample = read_traffic_sample()
                start_traffic_cycle(client, sample=sample)
                if not client.enable and client.disable_reason == "quota" and can_reenable(client):
                    client.enable, client.disable_reason = True, ""
                    changed = True
            reason = (
                "expiry"
                if client.expiry_time and client.expiry_time < now_ms
                else "quota"
                if client.limit_bytes and client.up + client.down >= client.limit_bytes
                else ""
            )
            if client.enable and reason:
                client.enable, client.disable_reason = False, reason
                changed = True
        if changed:
            prepare_runtime_config()
        revision = mark_runtime_dirty() if changed else None
        db.session.commit()
        synchronize_runtime(lambda: apply_client_activation_changes(activation_changes), expected_revision=revision)
    try:
        from panel_core.services.notifications import emit_if_new, evaluate_expiry

        for client_id in notify_ids:
            client = db.session.get(Client, client_id, populate_existing=True)
            if client is None or client.expiry_time is None:
                continue
            kind = evaluate_expiry(client, now_ms)
            if kind is not None:
                emit_if_new("expiry_notification", kind, client, {"expiry_time_ms": client.expiry_time})
    except Exception:
        logger.exception("Expiry notification pass failed")


def _read_log_chunk(kind, path):
    with open(path, "rb") as stream:
        stat = os.fstat(stream.fileno())
        checkpoint = db.session.get(LogCheckpoint, kind)
        if checkpoint is None:
            try:
                with open(
                    ACCESS_LOG_OFFSET_PATH if kind == "access" else REALITY_OFFSET_PATH, encoding="utf-8"
                ) as legacy:
                    initial = int(legacy.read().strip())
                if not 0 <= initial <= stat.st_size:
                    initial = 0
            except (OSError, ValueError):
                initial = 0
            checkpoint = LogCheckpoint(kind=kind, device=stat.st_dev, inode=stat.st_ino, offset=initial)
            db.session.add(checkpoint)
        offset = (
            checkpoint.offset
            if checkpoint.device == stat.st_dev
            and checkpoint.inode == stat.st_ino
            and checkpoint.offset <= stat.st_size
            else 0
        )
        stream.seek(offset)
        data = stream.read(4 * 1024 * 1024)
        end = data.rfind(b"\n") + 1
        checkpoint.device, checkpoint.inode, checkpoint.offset = stat.st_dev, stat.st_ino, offset + end
        return data[:end].decode("utf-8", errors="replace")


def _parse_access_logs_logic():
    if not os.path.exists(ACCESS_LOG_PATH):
        return
    with runtime_lock():
        logs = _read_log_chunk("access", ACCESS_LOG_PATH)
        for line in logs.splitlines():
            match = _ACCEPT_FULL.search(line)
            if match:
                ip, host, identity = match.groups()
                host = host.strip("[]")
            else:
                match = _ACCEPT_BASIC.search(line)
                if not match:
                    continue
                ip, identity = match.groups()
                host = None
            ip = ip.strip("[]")
            if not _is_ip_address(ip):
                continue
            tag, email = parse_runtime_email(identity)
            query = Client.query.filter_by(email=email)
            if tag:
                query = query.filter_by(inbound_tag=tag)
            try:
                seen = datetime.strptime(line[:19], "%Y/%m/%d %H:%M:%S")
            except ValueError:
                seen = datetime.now()
            for client in query.all():
                client.last_seen = max(client.last_seen or 0, int(seen.timestamp() * 1000))
                try:
                    ips = json.loads(client.source_ips or "[]")
                    if not isinstance(ips, list):
                        ips = []
                except (ValueError, TypeError):
                    ips = []
                client.source_ips = json.dumps(([ip] + [old for old in ips if old != ip])[:10])
                if host and not _is_ip_address(host):
                    _upsert_domain_stat(seen.date().isoformat(), host, email, client.inbound_tag, 1)
        db.session.commit()


def _collect_reality_failures_logic():
    if not os.path.exists(ERROR_LOG_PATH):
        return 0
    with runtime_lock():
        chunk = _read_log_chunk("reality", ERROR_LOG_PATH)
        count = record_failures(chunk.count(_REALITY_REFUSED), commit=False)
        db.session.commit()
        return count


def sync_traffic_job():
    with scheduler.app.app_context():
        sync_traffic_stats()


def check_limits_job():
    with scheduler.app.app_context():
        check_limits_and_reset()


def parse_access_logs():
    with scheduler.app.app_context():
        try:
            _parse_access_logs_logic()
            _collect_reality_failures_logic()
        except Exception:
            db.session.rollback()
            logger.exception("Xray log ingestion failed")
            raise
