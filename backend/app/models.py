import json
from .extensions import db


class Admin(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    password_changed_at = db.Column(db.BigInteger, nullable=False, default=0)


class RoutingProfile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    rules = db.Column(db.Text, nullable=False, default="[]")
    enable = db.Column(db.Boolean, nullable=False, default=True)
    inbounds = db.relationship("Inbound", backref="routing_profile", lazy=True)


class Outbound(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tag = db.Column(db.String(50), unique=True, nullable=False)
    protocol = db.Column(db.String(20), nullable=False, default="freedom")
    enable = db.Column(db.Boolean, nullable=False, default=True)
    settings = db.Column(db.Text, nullable=False, default="{}")
    stream_settings = db.Column(db.Text, nullable=False, default="{}")
    mux = db.Column(db.Text, nullable=False, default="{}")


class Balancer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tag = db.Column(db.String(50), unique=True, nullable=False)
    enable = db.Column(db.Boolean, nullable=False, default=True)
    selector = db.Column(db.Text, nullable=False, default="[]")
    strategy = db.Column(db.String(20), default="random")


class Inbound(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tag = db.Column(db.String(50), unique=True, nullable=False)
    port = db.Column(db.Integer, unique=True, nullable=False)
    protocol = db.Column(db.String(20), default="vless")
    stream_settings = db.Column(db.Text, nullable=False)
    routing_profile_id = db.Column(db.Integer, db.ForeignKey("routing_profile.id"), nullable=True)
    up = db.Column(db.BigInteger, default=0)
    down = db.Column(db.BigInteger, default=0)
    fallback_address = db.Column(db.String(100), nullable=True)
    clients = db.relationship("Client", backref="inbound", lazy=True, cascade="all, delete-orphan")


class Client(db.Model):
    id = db.Column(db.String(128), primary_key=True)
    email = db.Column(db.String(100), nullable=False)
    inbound_tag = db.Column(db.String(50), db.ForeignKey("inbound.tag"), nullable=False)
    limit_bytes = db.Column(db.BigInteger, default=0)
    expiry_time = db.Column(db.BigInteger, default=0)
    up = db.Column(db.BigInteger, default=0)
    down = db.Column(db.BigInteger, default=0)
    enable = db.Column(db.Boolean, default=True)
    reset_day = db.Column(db.Integer, default=0)
    last_reset_time = db.Column(db.BigInteger, default=0)
    last_seen = db.Column(db.BigInteger, default=0)
    source_ips = db.Column(db.Text, default="[]")
    flow = db.Column(db.String(50), nullable=True)
    preferred_outbound = db.Column(db.String(50), nullable=True)
    # Aggregate (sum across master + all nodes) traffic limit in bytes; 0 = unlimited.
    global_limit_bytes = db.Column(db.BigInteger, default=0)
    # Comma-separated list of node groups this user is allowed to see/use.
    # Empty string = no filter (all groups).
    allowed_node_groups = db.Column(db.Text, nullable=False, default="")

    def to_dict(self):
        ips = []
        try:
            if self.source_ips:
                ips = json.loads(self.source_ips)
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        groups = [g.strip() for g in (self.allowed_node_groups or "").split(",") if g.strip()]
        return {
            "id": self.id,
            "email": self.email,
            "inbound_tag": self.inbound_tag,
            "limit_bytes": self.limit_bytes,
            "expiry_time": self.expiry_time,
            "up": self.up,
            "down": self.down,
            "enable": self.enable,
            "reset_day": self.reset_day,
            "last_reset_time": self.last_reset_time,
            "last_seen": self.last_seen,
            "source_ips": ips,
            "flow": self.flow or "",
            "preferred_outbound": self.preferred_outbound or "",
            "global_limit_bytes": self.global_limit_bytes or 0,
            "allowed_node_groups": groups,
        }


class SystemSetting(db.Model):
    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, nullable=False, default="")


class TrafficSnapshot(db.Model):
    """Hourly traffic delta snapshots per entity (user or inbound)."""

    __tablename__ = "traffic_snapshot"
    id = db.Column(db.Integer, primary_key=True)
    entity_type = db.Column(db.String(10), nullable=False)  # 'inbound' or 'user'
    entity_id = db.Column(db.String(150), nullable=False)  # tag or email
    inbound_tag = db.Column(db.String(50), nullable=False, default="")  # '' for inbounds
    bucket = db.Column(db.BigInteger, nullable=False)  # unix ts of hour start
    up = db.Column(db.BigInteger, default=0)
    down = db.Column(db.BigInteger, default=0)
    __table_args__ = (
        db.UniqueConstraint("entity_type", "entity_id", "inbound_tag", "bucket", name="uq_ts"),
        db.Index("ix_ts_bucket", "bucket"),
        db.Index("ix_ts_entity", "entity_type", "entity_id", "inbound_tag"),
    )


class Node(db.Model):
    """Remote Xray panel node managed by this master panel."""

    __tablename__ = "node"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    url = db.Column(db.String(255), nullable=False)
    username = db.Column(db.String(50), nullable=False)
    password = db.Column(db.String(255), nullable=False)
    inbound_tag = db.Column(db.String(50), nullable=False)
    enable = db.Column(db.Boolean, default=True, nullable=False)
    sync_users = db.Column(db.Boolean, default=True, nullable=False)
    # Model default True for new rows; the migration adds the column with SQL default 0 so
    # existing nodes preserve the previous "no inbound sync" behaviour. See db_migration.py.
    sync_inbound = db.Column(db.Boolean, default=True, nullable=False)
    status = db.Column(db.String(20), default="unknown")
    last_check = db.Column(db.BigInteger, default=0)
    last_error = db.Column(db.Text, default="")
    # Comma-separated tags ("free,eu") used for per-user node group filtering.
    groups = db.Column(db.Text, nullable=False, default="")
    # When true, the reconcile job deletes remote users that aren't on master.
    strict_mirror = db.Column(db.Boolean, default=False, nullable=False)

    def to_dict(self, mask_password=True):
        groups = [g.strip() for g in (self.groups or "").split(",") if g.strip()]
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "username": self.username,
            "password": "••••••••" if mask_password else self.password,
            "inbound_tag": self.inbound_tag,
            "enable": self.enable,
            "sync_users": self.sync_users,
            "sync_inbound": self.sync_inbound,
            "status": self.status,
            "last_check": self.last_check,
            "last_error": self.last_error,
            "groups": groups,
            "strict_mirror": bool(self.strict_mirror),
        }


class NodeClientTraffic(db.Model):
    """Per-user traffic counter sampled from each remote node.

    Stores the latest absolute up/down values reported by the node so we can
    aggregate across all nodes for global enforcement and statistics.
    """

    __tablename__ = "node_client_traffic"
    id = db.Column(db.Integer, primary_key=True)
    node_id = db.Column(db.Integer, nullable=False)
    email = db.Column(db.String(100), nullable=False)
    up = db.Column(db.BigInteger, default=0)
    down = db.Column(db.BigInteger, default=0)
    last_polled = db.Column(db.BigInteger, default=0)
    __table_args__ = (
        db.UniqueConstraint("node_id", "email", name="uq_nct"),
        db.Index("ix_nct_email", "email"),
    )


class DomainStat(db.Model):
    """Daily domain access counts parsed from Xray access logs."""

    __tablename__ = "domain_stat"
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(10), nullable=False)  # YYYY-MM-DD
    domain = db.Column(db.String(255), nullable=False)
    client_email = db.Column(db.String(100), nullable=False, default="")
    inbound_tag = db.Column(db.String(50), nullable=False, default="")
    hit_count = db.Column(db.Integer, default=0)
    __table_args__ = (
        db.UniqueConstraint("date", "domain", "client_email", "inbound_tag", name="uq_ds"),
        db.Index("ix_ds_date", "date"),
        db.Index("ix_ds_domain", "domain"),
    )
