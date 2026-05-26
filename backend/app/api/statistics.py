import json

from flask import Blueprint, jsonify, request
from sqlalchemy import func, literal_column
from datetime import datetime

from app.extensions import db
from app.models import Client, Inbound, TrafficSnapshot, DomainStat
from app.utils import token_required

bp = Blueprint("statistics", __name__)

_PERIOD_SECONDS = {
    "1h": 3_600,
    "6h": 6 * 3_600,
    "24h": 24 * 3_600,
    "7d": 7 * 86_400,
    "30d": 30 * 86_400,
    "90d": 90 * 86_400,
    "365d": 365 * 86_400,
}


def _since_bucket(period: str):
    """Return (since_bucket_ts, since_date_str) or (None, None) for 'all'."""
    secs = _PERIOD_SECONDS.get(period)
    if secs is None:
        return None, None
    now_ts = int(datetime.now().timestamp())
    bucket = ((now_ts - secs) // 3600) * 3600
    date_str = datetime.fromtimestamp(bucket).date().isoformat()
    return bucket, date_str


def _resolve_range(args):
    """Return (since_bucket, since_date, until_bucket, until_date).

    If both 'from' and 'to' are present in the query they take precedence over
    'period' and define an [inclusive, exclusive) hourly-aligned window.
    Otherwise we fall through to the legacy 'period' preset and leave until_*
    None (open-ended — interpreted as "up to now" by the caller).

    Raises ValueError on malformed input.
    """
    raw_from = args.get("from")
    raw_to = args.get("to")
    if raw_from is not None or raw_to is not None:
        if raw_from is None or raw_to is None:
            raise ValueError("both 'from' and 'to' are required for a custom range")
        try:
            f_ts = int(raw_from)
            t_ts = int(raw_to)
        except (TypeError, ValueError) as exc:
            raise ValueError("'from' and 'to' must be unix-seconds integers") from exc
        if t_ts <= f_ts:
            raise ValueError("'to' must be greater than 'from'")
        since_bucket = (f_ts // 3600) * 3600
        until_bucket = (t_ts // 3600) * 3600
        if until_bucket == since_bucket:
            until_bucket += 3600
        since_date = datetime.fromtimestamp(since_bucket).date().isoformat()
        until_date = datetime.fromtimestamp(until_bucket).date().isoformat()
        return since_bucket, since_date, until_bucket, until_date
    period = args.get("period", "7d")
    since_bucket, since_date = _since_bucket(period)
    return since_bucket, since_date, None, None


def _granularity_for_duration(secs: int) -> int:
    """Bucket size in seconds for aggregating chart points, given a window duration."""
    if secs <= 3_600:
        return 600  # 10-minute buckets → 6 points per hour
    if secs <= 86_400:
        return 3_600  # hourly
    if secs <= 90 * 86_400:
        return 86_400  # daily
    return 7 * 86_400  # weekly


def _granularity_seconds(period: str) -> int:
    """Legacy preset-based granularity, retained for the no-custom-range path."""
    secs = _PERIOD_SECONDS.get(period)
    if secs is None:
        return 7 * 86_400  # 'all' → weekly
    return _granularity_for_duration(secs)


@bp.get("/stats/overview")
@token_required
def get_overview():
    try:
        since_bucket, since_date, until_bucket, until_date = _resolve_range(request.args)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    clients = Client.query.all()
    inbounds = Inbound.query.all()
    ib_protocols = {ib.tag: ib.protocol for ib in inbounds}

    # All-time totals straight from the models
    total_up_alltime = sum(c.up for c in clients)
    total_down_alltime = sum(c.down for c in clients)

    # Period traffic from snapshots
    user_snaps_q = db.session.query(
        TrafficSnapshot.entity_id,
        TrafficSnapshot.inbound_tag,
        func.sum(TrafficSnapshot.up).label("up"),
        func.sum(TrafficSnapshot.down).label("down"),
    ).filter(TrafficSnapshot.entity_type == "user")
    if since_bucket is not None:
        user_snaps_q = user_snaps_q.filter(TrafficSnapshot.bucket >= since_bucket)
    if until_bucket is not None:
        user_snaps_q = user_snaps_q.filter(TrafficSnapshot.bucket < until_bucket)
    user_snaps = user_snaps_q.group_by(TrafficSnapshot.entity_id, TrafficSnapshot.inbound_tag).all()

    period_up = sum(r.up for r in user_snaps)
    period_down = sum(r.down for r in user_snaps)

    top_users = sorted(
        [
            {
                "email": r.entity_id,
                "inbound_tag": r.inbound_tag,
                "up": r.up,
                "down": r.down,
                "total": r.up + r.down,
            }
            for r in user_snaps
        ],
        key=lambda x: x["total"],
        reverse=True,
    )[:10]

    ib_snaps_q = db.session.query(
        TrafficSnapshot.entity_id,
        func.sum(TrafficSnapshot.up).label("up"),
        func.sum(TrafficSnapshot.down).label("down"),
    ).filter(TrafficSnapshot.entity_type == "inbound")
    if since_bucket is not None:
        ib_snaps_q = ib_snaps_q.filter(TrafficSnapshot.bucket >= since_bucket)
    if until_bucket is not None:
        ib_snaps_q = ib_snaps_q.filter(TrafficSnapshot.bucket < until_bucket)
    ib_snaps = ib_snaps_q.group_by(TrafficSnapshot.entity_id).all()

    top_inbounds = sorted(
        [
            {
                "tag": r.entity_id,
                "protocol": ib_protocols.get(r.entity_id, "unknown"),
                "up": r.up,
                "down": r.down,
                "total": r.up + r.down,
            }
            for r in ib_snaps
        ],
        key=lambda x: x["total"],
        reverse=True,
    )

    # Top domains in period
    domain_q = db.session.query(
        DomainStat.domain,
        func.sum(DomainStat.hit_count).label("hits"),
    )
    if since_date is not None:
        domain_q = domain_q.filter(DomainStat.date >= since_date)
    if until_date is not None:
        domain_q = domain_q.filter(DomainStat.date <= until_date)
    domain_q = domain_q.group_by(DomainStat.domain).order_by(func.sum(DomainStat.hit_count).desc()).limit(10)
    top_domains = [{"domain": r.domain, "hit_count": r.hits} for r in domain_q.all()]

    return jsonify(
        {
            "total_up_alltime": total_up_alltime,
            "total_down_alltime": total_down_alltime,
            "period_up": period_up,
            "period_down": period_down,
            "active_users": sum(1 for c in clients if c.enable),
            "total_users": len(clients),
            "active_inbounds": len(inbounds),
            "top_users": top_users,
            "top_inbounds": top_inbounds,
            "top_domains": top_domains,
        }
    )


@bp.get("/stats/traffic")
@token_required
def get_traffic():
    """Time-series traffic chart data.

    Query params:
      period     – 1h|6h|24h|7d|30d|90d|365d|all  (default: 7d)
      entity_type – inbound|user|all  (default: all)
      entity_id   – tag or email (required when entity_type != all)
      inbound_tag – inbound tag filter when entity_type == user
    """
    period = request.args.get("period", "7d")
    entity_type = request.args.get("entity_type", "all")
    entity_id = request.args.get("entity_id", "")
    inbound_tag = request.args.get("inbound_tag", "")

    try:
        since_bucket, _, until_bucket, _ = _resolve_range(request.args)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if until_bucket is not None and since_bucket is not None:
        gran = _granularity_for_duration(until_bucket - since_bucket)
    else:
        gran = _granularity_seconds(period)

    bucket_expr = literal_column(f"(bucket / {gran}) * {gran}")

    q = db.session.query(
        bucket_expr.label("ts"),
        func.sum(TrafficSnapshot.up).label("up"),
        func.sum(TrafficSnapshot.down).label("down"),
    )

    if entity_type == "inbound":
        q = q.filter(
            TrafficSnapshot.entity_type == "inbound",
            TrafficSnapshot.entity_id == entity_id,
        )
    elif entity_type == "user":
        q = q.filter(TrafficSnapshot.entity_type == "user")
        if entity_id:
            q = q.filter(TrafficSnapshot.entity_id == entity_id)
        if inbound_tag:
            q = q.filter(TrafficSnapshot.inbound_tag == inbound_tag)
    else:
        # all – aggregate everything (use user snapshots to avoid double counting)
        q = q.filter(TrafficSnapshot.entity_type == "user")

    if since_bucket is not None:
        q = q.filter(TrafficSnapshot.bucket >= since_bucket)
    if until_bucket is not None:
        q = q.filter(TrafficSnapshot.bucket < until_bucket)

    rows = q.group_by(bucket_expr).order_by(bucket_expr).all()

    return jsonify(
        {
            "granularity": gran,
            "points": [{"ts": int(r.ts), "up": int(r.up), "down": int(r.down)} for r in rows],
        }
    )


@bp.get("/stats/domains")
@token_required
def get_domains():
    """Top domains with optional filters."""
    limit = min(int(request.args.get("limit", 50)), 200)
    email_filter = request.args.get("email", "")
    tag_filter = request.args.get("inbound_tag", "")
    try:
        _, since_date, _, until_date = _resolve_range(request.args)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    q = db.session.query(
        DomainStat.domain,
        func.sum(DomainStat.hit_count).label("hits"),
    )
    if since_date:
        q = q.filter(DomainStat.date >= since_date)
    if until_date:
        q = q.filter(DomainStat.date <= until_date)
    if email_filter:
        q = q.filter(DomainStat.client_email == email_filter)
    if tag_filter:
        q = q.filter(DomainStat.inbound_tag == tag_filter)

    rows = q.group_by(DomainStat.domain).order_by(func.sum(DomainStat.hit_count).desc()).limit(limit).all()

    total = sum(r.hits for r in rows) or 1
    return jsonify(
        {
            "domains": [
                {
                    "domain": r.domain,
                    "hit_count": r.hits,
                    "percent": round(r.hits / total * 100, 1),
                }
                for r in rows
            ]
        }
    )


@bp.get("/stats/domain-users")
@token_required
def get_domain_users():
    """Per-user breakdown for a specific domain."""
    domain = request.args.get("domain", "").strip()
    if not domain:
        return jsonify({"error": "domain is required"}), 400

    try:
        _, since_date, _, until_date = _resolve_range(request.args)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    q = db.session.query(
        DomainStat.client_email,
        DomainStat.inbound_tag,
        func.sum(DomainStat.hit_count).label("hits"),
    ).filter(DomainStat.domain == domain)

    if since_date:
        q = q.filter(DomainStat.date >= since_date)
    if until_date:
        q = q.filter(DomainStat.date <= until_date)

    rows = (
        q.group_by(DomainStat.client_email, DomainStat.inbound_tag)
        .order_by(func.sum(DomainStat.hit_count).desc())
        .all()
    )

    total = sum(r.hits for r in rows) or 1
    return jsonify(
        {
            "domain": domain,
            "users": [
                {
                    "email": r.client_email or "(unknown)",
                    "inbound_tag": r.inbound_tag or "",
                    "hit_count": r.hits,
                    "percent": round(r.hits / total * 100, 1),
                }
                for r in rows
            ],
        }
    )


@bp.get("/stats/users-ranking")
@token_required
def get_users_ranking():
    """Users ranked by traffic in the selected period."""
    try:
        since_bucket, _, until_bucket, _ = _resolve_range(request.args)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    rows = db.session.query(
        TrafficSnapshot.entity_id.label("email"),
        TrafficSnapshot.inbound_tag,
        func.sum(TrafficSnapshot.up).label("up"),
        func.sum(TrafficSnapshot.down).label("down"),
    ).filter(TrafficSnapshot.entity_type == "user")

    if since_bucket is not None:
        rows = rows.filter(TrafficSnapshot.bucket >= since_bucket)
    if until_bucket is not None:
        rows = rows.filter(TrafficSnapshot.bucket < until_bucket)

    rows = (
        rows.group_by(TrafficSnapshot.entity_id, TrafficSnapshot.inbound_tag)
        .order_by((func.sum(TrafficSnapshot.up) + func.sum(TrafficSnapshot.down)).desc())
        .all()
    )

    # Enrich with current DB status
    client_map = {(c.email, c.inbound_tag): c for c in Client.query.all()}

    result = []
    for r in rows:
        c = client_map.get((r.email, r.inbound_tag))
        try:
            ips = json.loads(c.source_ips) if (c and c.source_ips) else []
        except (TypeError, ValueError, json.JSONDecodeError):
            ips = []
        result.append(
            {
                "email": r.email,
                "inbound_tag": r.inbound_tag,
                "up": int(r.up),
                "down": int(r.down),
                "total": int(r.up) + int(r.down),
                "enable": c.enable if c else True,
                "last_seen": c.last_seen if c else 0,
                "limit_bytes": c.limit_bytes if c else 0,
                "source_ips": ips,
            }
        )

    return jsonify({"users": result})
