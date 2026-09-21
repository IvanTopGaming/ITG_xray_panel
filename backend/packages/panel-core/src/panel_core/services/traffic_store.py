import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import text

from panel_core.extensions import db, scheduler
from panel_core.models import AccountAccessState, Client, DomainStat, Inbound, NotificationLog, TrafficCounterBaseline
from panel_core.services.runtime_apply import (
    mark_runtime_dirty,
    prepare_runtime_config,
    runtime_lock,
    synchronize_runtime,
)
from panel_core.services.runtime_identity import build_runtime_email
from panel_core.xray.gateway import get_xray_gateway

logger = logging.getLogger(__name__)


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


def cleanup_old_domain_stats():

    try:
        cutoff = (datetime.now() - timedelta(days=90)).date().isoformat()
        deleted = DomainStat.query.filter(DomainStat.date < cutoff).delete()
        if deleted:
            db.session.commit()
            logger.info("Cleaned up %d old domain stat rows", deleted)
    except Exception as e:
        logger.info("Domain stat cleanup failed: %s", e)


def cleanup_stats_job():
    with scheduler.app.app_context():
        cleanup_old_domain_stats()


def read_traffic_sample():
    gateway = get_xray_gateway()
    if not gateway.has_local_xray():
        return None
    epoch, values = gateway.read_traffic_counters()
    if not epoch:
        raise RuntimeError("Xray runtime epoch is unavailable")
    generations = {
        client_id: generation or ""
        for client_id, generation in db.session.query(Client.id, Client.traffic_generation).all()
    }
    return epoch, values, generations


def _settle_traffic(entity, entity_type, identity, sample):
    if sample is None:
        return 0, 0
    epoch, values, generations = sample
    generation = entity.traffic_generation or "" if entity_type == "user" else ""
    if entity_type == "user" and generations.get(entity.id) != generation:
        raise RuntimeError("Traffic generation changed; retry the sample")
    prefix = f"{entity_type}>>>{identity}>>>traffic>>>"
    raw = [int(values.get(prefix + direction, 0)) for direction in ("uplink", "downlink")]
    if any(value < 0 for value in raw):
        raise ValueError("Negative traffic counter")
    entity_id = entity.id if entity_type == "user" else entity.tag
    tag = entity.inbound_tag if entity_type == "user" else ""
    key = (entity_type, entity_id, tag)
    baseline = db.session.get(TrafficCounterBaseline, key)
    previous = [0, 0]
    if baseline is not None and baseline.runtime_epoch == epoch and baseline.runtime_identity == identity:
        if baseline.traffic_generation != generation:
            raise RuntimeError("Traffic cycle has no durable counter boundary")
        previous = [baseline.up, baseline.down]
        raw = [
            value if prefix + direction in values else old
            for value, old, direction in zip(raw, previous, ("uplink", "downlink"))
        ]
    delta = [value - old if value >= old else value for value, old in zip(raw, previous)]
    if baseline is None:
        baseline = TrafficCounterBaseline(entity_type=entity_type, entity_id=entity_id, inbound_tag=tag)
        db.session.add(baseline)
    baseline.runtime_epoch, baseline.runtime_identity = epoch, identity
    baseline.up, baseline.down = raw
    baseline.traffic_generation = generation
    if entity.up is None or entity.down is None or entity.up < 0 or entity.down < 0:
        raise ValueError("Invalid stored traffic counters")
    entity.up += delta[0]
    entity.down += delta[1]
    _upsert_snapshot(
        entity_type, entity.email if entity_type == "user" else entity.tag, tag, _ten_min_bucket(datetime.now()), *delta
    )
    return tuple(delta)


def settle_client_traffic(client, *, sample=None):
    with runtime_lock():
        return _settle_traffic(
            client,
            "user",
            build_runtime_email(client.inbound_tag, client.email),
            read_traffic_sample() if sample is None else sample,
        )


def settle_inbound_traffic(inbound, *, sample):
    return _settle_traffic(inbound, "inbound", inbound.tag, sample)


def start_traffic_cycle(client, *, generation=None, sample=None, reset_usage=True):
    with runtime_lock():
        sample = read_traffic_sample() if sample is None else sample
        settle_client_traffic(client, sample=sample)
        client.traffic_generation = generation or uuid.uuid4().hex
        client.last_reset_time = int(datetime.now().timestamp() * 1000)
        if reset_usage:
            client.up = client.down = 0
        baseline = db.session.get(TrafficCounterBaseline, ("user", client.id, client.inbound_tag))
        if baseline is not None:
            baseline.traffic_generation = client.traffic_generation
        NotificationLog.query.filter(
            NotificationLog.client_id == client.id, NotificationLog.kind.like("traffic_%")
        ).delete(synchronize_session=False)


def can_reenable(client):
    account = db.session.get(AccountAccessState, client.telegram_id) if client.telegram_id is not None else None
    return (
        not client.manual_disabled
        and not (account and account.blocked)
        and client.expiry_time is not None
        and (client.expiry_time == 0 or client.expiry_time > int(datetime.now().timestamp() * 1000))
    )


def reset_user_traffic(tag, email, *, reenable=False):
    from panel_core.services.entitlements import apply_client_activation_changes

    with runtime_lock():
        client = Client.query.filter_by(inbound_tag=tag, email=email).populate_existing().first()
        if not client:
            raise ValueError("User not found")
        was_enabled = bool(client.enable)
        start_traffic_cycle(client)
        changed = reenable and not client.enable and can_reenable(client)
        if changed:
            client.enable, client.disable_reason = True, ""
            prepare_runtime_config()
        revision = mark_runtime_dirty() if changed else None
        db.session.commit()
        synchronize_runtime(
            lambda: apply_client_activation_changes([(client, was_enabled)]), expected_revision=revision
        )


def reset_inbound_traffic(tag):
    with runtime_lock():
        ib = Inbound.query.filter_by(tag=tag).populate_existing().first()
        if not ib:
            raise ValueError("Inbound not found")
        sample = read_traffic_sample()
        for client in ib.clients:
            start_traffic_cycle(client, sample=sample)
        settle_inbound_traffic(ib, sample=sample)
        ib.up = ib.down = 0
        db.session.commit()


def bulk_delete_users(users_list):
    if not users_list:
        return 0
    with runtime_lock():
        gateway = get_xray_gateway()
        clients = []
        seen = set()
        for user in users_list:
            key = (user.get("tag"), user.get("email"))
            if not all(key) or key in seen:
                continue
            seen.add(key)
            client = Client.query.filter_by(inbound_tag=key[0], email=key[1]).first()
            if client is not None:
                clients.append(client)
        if not clients:
            return 0
        sample = read_traffic_sample()
        for client in clients:
            settle_client_traffic(client, sample=sample)
            db.session.delete(client)
        revision = None
        if gateway.has_local_xray():
            prepare_runtime_config()
            revision = mark_runtime_dirty()
        db.session.commit()
        if gateway.has_local_xray():
            synchronize_runtime(expected_revision=revision)
        return len(clients)
