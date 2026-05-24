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
    fallback_tag = db.Column(db.String(50), nullable=True)


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
    device_limit = db.Column(db.Integer, default=0, nullable=False)
    label = db.Column(db.String(60), nullable=True)
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
    device_limit = db.Column(db.Integer, nullable=True)
    telegram_id = db.Column(db.BigInteger, nullable=True, index=True)
    tariff_id = db.Column(
        db.Integer,
        db.ForeignKey("tariff.id"),
        nullable=True,
        index=True,
    )
    devices = db.relationship("ClientDevice", backref="client", lazy=True, cascade="all, delete-orphan")

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
            "inbound_label": (self.inbound.label if self.inbound else None) or self.inbound_tag,
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
            "device_limit": self.device_limit,
            "telegram_id": self.telegram_id,
            "tariff_id": self.tariff_id,
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


class ClientDevice(db.Model):
    """Track devices (HWIDs) for each client, including OS and hardware info."""

    __tablename__ = "client_device"
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(
        db.String(128),
        db.ForeignKey("client.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    hwid = db.Column(db.String(128), nullable=False)
    device_os = db.Column(db.String(32), default="")
    os_ver = db.Column(db.String(32), default="")
    model = db.Column(db.String(128), default="")
    user_agent = db.Column(db.String(512), default="")
    request_ip = db.Column(db.String(64), default="")
    first_seen = db.Column(db.BigInteger, nullable=False)
    last_seen = db.Column(db.BigInteger, nullable=False)
    hits = db.Column(db.Integer, default=1)

    __table_args__ = (db.UniqueConstraint("client_id", "hwid", name="uq_client_hwid"),)

    def to_dict(self, *, include_admin_fields=False):
        out = {
            "id": self.id,
            "device_os": self.device_os or "",
            "os_ver": self.os_ver or "",
            "model": self.model or "",
            "first_seen": int(self.first_seen or 0),
            "last_seen": int(self.last_seen or 0),
        }
        if include_admin_fields:
            out["hwid"] = self.hwid
            out["user_agent"] = self.user_agent or ""
            out["request_ip"] = self.request_ip or ""
            out["hits"] = int(self.hits or 0)
        return out


# ─── Billing: tariffs ────────────────────────────────────────────────────


class Tariff(db.Model):
    __tablename__ = "tariff"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    price_rub = db.Column(db.Integer, nullable=False)
    period_days = db.Column(db.Integer, nullable=False)
    visibility = db.Column(db.String(16), nullable=False, default="public")
    # 'public' | 'private' | 'archived'
    is_trial = db.Column(db.Boolean, nullable=False, default=False)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    updated_at = db.Column(
        db.DateTime,
        default=db.func.current_timestamp(),
        onupdate=db.func.current_timestamp(),
    )

    items = db.relationship(
        "TariffItem",
        backref="tariff",
        cascade="all, delete-orphan",
        order_by="TariffItem.sort_order",
    )

    __table_args__ = (db.Index("ix_tariff_visibility", "visibility"),)


class TariffItem(db.Model):
    __tablename__ = "tariff_item"

    id = db.Column(db.Integer, primary_key=True)
    tariff_id = db.Column(
        db.Integer,
        db.ForeignKey("tariff.id", ondelete="CASCADE"),
        nullable=False,
    )
    inbound_tag = db.Column(db.String(120), nullable=False)
    label = db.Column(db.String(60), nullable=True)
    traffic_gb = db.Column(db.Integer, nullable=False)  # 0 = unlimited
    allowed_node_groups = db.Column(db.String(255), nullable=False, default="")
    sort_order = db.Column(db.Integer, nullable=False, default=0)

    __table_args__ = (db.Index("ix_tariff_item_tariff", "tariff_id"),)


class UserTariffAccess(db.Model):
    __tablename__ = "user_tariff_access"

    id = db.Column(db.Integer, primary_key=True)
    telegram_id = db.Column(db.BigInteger, nullable=False)
    tariff_id = db.Column(
        db.Integer,
        db.ForeignKey("tariff.id", ondelete="CASCADE"),
        nullable=False,
    )
    billing = db.Column(db.String(8), nullable=False)  # 'free' | 'paid'
    next_renewal_at = db.Column(db.DateTime, nullable=True)
    note = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())

    __table_args__ = (
        db.UniqueConstraint("telegram_id", "tariff_id", name="uq_user_tariff"),
        db.Index("ix_uta_telegram", "telegram_id"),
        db.Index("ix_uta_renewal", "next_renewal_at"),
    )


class Payment(db.Model):
    __tablename__ = "payment"

    id = db.Column(db.Integer, primary_key=True)
    yookassa_id = db.Column(db.String(64), unique=True, nullable=False)
    telegram_id = db.Column(db.BigInteger, nullable=False, index=True)
    tariff_id = db.Column(
        db.Integer,
        db.ForeignKey("tariff.id"),  # default RESTRICT — preserves history
        nullable=False,
    )
    tariff_snapshot = db.Column(db.JSON, nullable=False)
    amount_rub = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(16), nullable=False)
    # 'pending' | 'succeeded' | 'cancelled' | 'failed'
    confirmation_url = db.Column(db.Text, nullable=True)
    metadata_json = db.Column("metadata", db.JSON, nullable=False, default=dict, server_default="{}")
    created_at = db.Column(
        db.DateTime,
        default=db.func.current_timestamp(),
        index=True,
    )
    paid_at = db.Column(db.DateTime, nullable=True)
    chat_id = db.Column(db.BigInteger, nullable=True)
    message_id = db.Column(db.Integer, nullable=True)


class BotText(db.Model):
    __tablename__ = "bot_text"

    key = db.Column(db.String(120), primary_key=True)
    lang = db.Column(db.String(8), primary_key=True)
    text = db.Column(db.Text, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=db.func.current_timestamp(),
        onupdate=db.func.current_timestamp(),
    )


class BotEvent(db.Model):
    __tablename__ = "bot_event"

    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(32), nullable=False)
    telegram_id = db.Column(db.BigInteger, nullable=True, index=True)
    # nullable telegram_id = broadcast event (e.g., 'texts_changed')
    payload = db.Column(db.JSON, nullable=False)
    created_at = db.Column(
        db.DateTime,
        default=db.func.current_timestamp(),
        index=True,
    )
    delivered_at = db.Column(db.DateTime, nullable=True)


class TelegramUser(db.Model):
    __tablename__ = "telegram_user"

    telegram_id = db.Column(db.BigInteger, primary_key=True)
    username = db.Column(db.String(64), nullable=True)
    language = db.Column(db.String(8), nullable=False, default="ru")
    trial_used_at = db.Column(db.DateTime, nullable=True)
    blocked = db.Column(db.Boolean, nullable=False, default=False)
    language_chosen = db.Column(db.Boolean, nullable=False, default=False)
    first_seen_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    last_seen_at = db.Column(
        db.DateTime,
        default=db.func.current_timestamp(),
        onupdate=db.func.current_timestamp(),
    )
    note = db.Column(db.String(255), nullable=True)


class NotificationLog(db.Model):
    __tablename__ = "notification_log"

    id = db.Column(db.Integer, primary_key=True)
    telegram_id = db.Column(db.BigInteger, nullable=False, index=True)
    client_id = db.Column(
        db.String(128),
        db.ForeignKey("client.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind = db.Column(db.String(32), nullable=False)
    # 'expiry_3d' | 'expiry_1d' | 'expiry_1h' | 'expired'
    sent_at = db.Column(db.DateTime, default=db.func.current_timestamp())

    __table_args__ = (db.Index("ix_notif_dedup", "telegram_id", "client_id", "kind", "sent_at"),)
