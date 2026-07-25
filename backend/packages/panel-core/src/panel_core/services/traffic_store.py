import logging
from datetime import datetime, timedelta

from sqlalchemy import text

from panel_core.extensions import db, scheduler
from panel_core.models import Client, DomainStat, Inbound
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


def reset_user_traffic(tag, email):
    client = Client.query.filter_by(inbound_tag=tag, email=email).first()
    if not client:
        raise Exception("User not found")
    runtime_email = build_runtime_email(tag, email)
    gateway = get_xray_gateway()
    if gateway.has_local_xray():
        gateway.reset_user_counters(tag, email, runtime_email)
    client.up = 0
    client.down = 0
    db.session.commit()


def reset_inbound_traffic(tag):
    ib = Inbound.query.filter_by(tag=tag).first()
    if not ib:
        raise Exception("Inbound not found")
    for client in ib.clients:
        reset_user_traffic(tag, client.email)
    gateway = get_xray_gateway()
    if gateway.has_local_xray():
        gateway.reset_inbound_counters(tag)
    ib.up = 0
    ib.down = 0
    db.session.commit()


def bulk_delete_users(users_list):
    if not users_list:
        return 0

    gateway = get_xray_gateway()
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
    gateway.apply_config()

    if restart_required:
        gateway.restart()
    else:
        grpc_failed = False
        for tag, email in grpc_removals:
            if not gateway.remove_user(tag, email):
                grpc_failed = True
        if grpc_failed:
            gateway.restart()

    return deleted_count
