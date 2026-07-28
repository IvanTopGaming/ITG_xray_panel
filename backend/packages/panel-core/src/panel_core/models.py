import json
import uuid

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
    send_through = db.Column(db.String(50), nullable=True)
    public_ip = db.Column(db.String(50), nullable=True)
    gateway = db.Column(db.String(50), nullable=True)


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
    device_limit = db.Column(db.Integer, nullable=True)
    telegram_id = db.Column(db.BigInteger, nullable=True, index=True)
    tariff_id = db.Column(
        db.Integer,
        db.ForeignKey("tariff.id"),
        nullable=True,
        index=True,
    )

    def to_dict(self):
        ips = []
        try:
            if self.source_ips:
                ips = json.loads(self.source_ips)
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
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
            "device_limit": self.device_limit,
            "telegram_id": self.telegram_id,
            "tariff_id": self.tariff_id,
        }


class SystemSetting(db.Model):
    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, nullable=False, default="")


class TrafficSnapshot(db.Model):
    __tablename__ = "traffic_snapshot"
    id = db.Column(db.Integer, primary_key=True)
    entity_type = db.Column(db.String(10), nullable=False)
    entity_id = db.Column(db.String(150), nullable=False)
    inbound_tag = db.Column(db.String(50), nullable=False, default="")
    bucket = db.Column(db.BigInteger, nullable=False)
    up = db.Column(db.BigInteger, default=0)
    down = db.Column(db.BigInteger, default=0)
    __table_args__ = (
        db.UniqueConstraint("entity_type", "entity_id", "inbound_tag", "bucket", name="uq_ts"),
        db.Index("ix_ts_bucket", "bucket"),
        db.Index("ix_ts_entity", "entity_type", "entity_id", "inbound_tag"),
        db.Index("ix_ts_type_bucket", "entity_type", "bucket"),
        db.Index("ix_ts_type_bucket_cover", "entity_type", "bucket", "entity_id", "inbound_tag", "up", "down"),
    )


class LinkedPanel(db.Model):
    __tablename__ = "linked_panel"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    url = db.Column(db.String(255), nullable=False)
    federation_token = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(20), default="unknown", nullable=False)
    last_poll = db.Column(db.BigInteger, nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    enable = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.BigInteger, nullable=False)

    def to_dict(self, mask_token=True):
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "federation_token": "••••••••" if mask_token else self.federation_token,
            "status": self.status,
            "last_poll": self.last_poll,
            "last_error": self.last_error,
            "enable": bool(self.enable),
            "created_at": self.created_at,
        }


class FederationConfig(db.Model):
    __tablename__ = "federation_config"
    id = db.Column(db.Integer, primary_key=True)
    master_url = db.Column(db.String(255), nullable=True)
    master_name = db.Column(db.String(100), nullable=True)
    federation_token = db.Column(db.String(255), nullable=True)
    link_token = db.Column(db.String(255), nullable=True)
    link_token_used = db.Column(db.Boolean, default=False, nullable=False)
    linked_at = db.Column(db.BigInteger, nullable=True)
    __table_args__ = (db.CheckConstraint("id = 1", name="singleton_federation_config"),)


class DomainStat(db.Model):
    __tablename__ = "domain_stat"
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(10), nullable=False)
    domain = db.Column(db.String(255), nullable=False)
    client_email = db.Column(db.String(100), nullable=False, default="")
    inbound_tag = db.Column(db.String(50), nullable=False, default="")
    hit_count = db.Column(db.Integer, default=0)
    __table_args__ = (
        db.UniqueConstraint("date", "domain", "client_email", "inbound_tag", name="uq_ds"),
        db.Index("ix_ds_date", "date"),
        db.Index("ix_ds_domain", "domain"),
        db.Index("ix_ds_date_domain", "date", "domain"),
        db.Index("ix_ds_date_domain_cover", "date", "domain", "client_email", "inbound_tag", "hit_count"),
    )


class UserDevice(db.Model):
    __tablename__ = "user_device"
    id = db.Column(db.Integer, primary_key=True)
    telegram_id = db.Column(db.BigInteger, nullable=False, index=True)
    hwid = db.Column(db.String(128), nullable=False)
    device_os = db.Column(db.String(32), default="")
    os_ver = db.Column(db.String(32), default="")
    model = db.Column(db.String(128), default="")
    user_agent = db.Column(db.String(512), default="")
    request_ip = db.Column(db.String(64), default="")
    first_seen = db.Column(db.BigInteger, nullable=False)
    last_seen = db.Column(db.BigInteger, nullable=False)
    hits = db.Column(db.Integer, default=1)

    __table_args__ = (db.UniqueConstraint("telegram_id", "hwid", name="uq_user_hwid"),)

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


class Tariff(db.Model):
    __tablename__ = "tariff"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    price_rub = db.Column(db.Integer, nullable=False)
    period_days = db.Column(db.Integer, nullable=False)
    visibility = db.Column(db.String(16), nullable=False, default="public")

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
    traffic_gb = db.Column(db.Integer, nullable=False)
    panel_id = db.Column(db.Integer, db.ForeignKey("linked_panel.id"), nullable=True)
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
    billing = db.Column(db.String(8), nullable=False)
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
        db.ForeignKey("tariff.id"),
        nullable=False,
    )
    tariff_snapshot = db.Column(db.JSON, nullable=False)
    amount_rub = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(16), nullable=False)

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

    customized = db.Column(db.Boolean, nullable=False, default=False)
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
    sub_token = db.Column(
        db.String(36),
        unique=True,
        nullable=True,
        index=True,
        default=lambda: str(uuid.uuid4()),
    )


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

    sent_at = db.Column(db.DateTime, default=db.func.current_timestamp())

    __table_args__ = (db.Index("ix_notif_dedup", "telegram_id", "client_id", "kind", "sent_at"),)


class NotificationClaim(db.Model):
    __tablename__ = "notification_claim"

    id = db.Column(db.Integer, primary_key=True)
    telegram_id = db.Column(db.BigInteger, nullable=False)
    tariff_id = db.Column(db.Integer, nullable=False, default=0)
    scope = db.Column(db.String(200), nullable=False, default="")
    kind = db.Column(db.String(32), nullable=False)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())

    __table_args__ = (db.UniqueConstraint("telegram_id", "tariff_id", "scope", "kind", name="uq_notification_claim"),)


class ProvisionReceipt(db.Model):
    __tablename__ = "provision_receipt"

    id = db.Column(db.Integer, primary_key=True)
    idempotency_key = db.Column(db.String(128), nullable=False)
    inbound_tag = db.Column(db.String(50), nullable=False)
    telegram_id = db.Column(db.BigInteger, nullable=False)
    response_json = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())

    __table_args__ = (db.UniqueConstraint("idempotency_key", "inbound_tag", name="uq_provision_receipt"),)
