import os
import secrets
import jwt
import re
import tempfile
from functools import wraps
from flask import request, jsonify


def get_or_create_secret_key():
    env_key = os.getenv("SECRET_KEY")
    if env_key:
        return env_key.strip()
    key_file = os.path.join(os.getcwd(), "secret.key")
    if os.path.exists(key_file):
        try:
            with open(key_file, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            pass
    new_key = secrets.token_hex(32)
    fd = None
    temp_path = ""
    try:
        fd, temp_path = tempfile.mkstemp(
            prefix="secret.",
            suffix=".key",
            dir=os.path.dirname(key_file) or ".",
            text=True,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = None
            f.write(new_key)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, key_file)
    except OSError:
        if fd is not None:
            os.close(fd)
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        pass
    return new_key


SECRET_KEY = get_or_create_secret_key()
TAG_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,50}$")
EMAIL_PATTERN = re.compile(r"^[^\s\x00-\x1F\x7F]{1,100}$")


def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        if "Authorization" in request.headers:
            auth_header = str(request.headers.get("Authorization", "")).strip()
            scheme, _, value = auth_header.partition(" ")
            if scheme.lower() == "bearer" and value.strip():
                token = value.strip()
        if not token:
            return jsonify({"message": "Token is missing!"}), 401
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            return jsonify({"message": "Token is expired!"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"message": "Token is invalid!"}), 401

        if payload.get("role") != "admin":
            return jsonify({"message": "Token is invalid!"}), 401

        from app.models import Admin

        admin = None
        admin_id = payload.get("admin_id")
        if admin_id is not None:
            try:
                from app.extensions import db

                admin = db.session.get(Admin, int(admin_id))
            except (TypeError, ValueError):
                admin = None
        if admin is None and payload.get("user"):
            admin = Admin.query.filter_by(username=payload.get("user")).first()
        if admin is None:
            return jsonify({"message": "Token is invalid!"}), 401

        token_pwd_version = payload.get("pwdv")
        if token_pwd_version is None:
            return jsonify({"message": "Token is invalid!"}), 401
        try:
            token_pwd_version = int(token_pwd_version)
        except (TypeError, ValueError):
            return jsonify({"message": "Token is invalid!"}), 401

        current_pwd_version = int(admin.password_changed_at or 0)
        if token_pwd_version != current_pwd_version:
            return jsonify({"message": "Token is invalid!"}), 401
        return f(*args, **kwargs)

    return decorated


def bot_service_token_required(f):
    """Bearer = SystemSetting('bot_service_token'). 401 on miss, 500 if the setting row is absent."""
    import secrets as _secrets

    @wraps(f)
    def decorated(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return jsonify({"error": "missing or malformed authorization"}), 401
        token = header[len("Bearer ") :].strip()

        from app.models import SystemSetting

        setting = SystemSetting.query.filter_by(key="bot_service_token").first()
        if setting is None or not setting.value:
            return jsonify({"error": "bot_service_token not configured"}), 500
        # Constant-time compare guards against timing attacks (internal-network
        # only, but cheap to harden).
        if not _secrets.compare_digest(token, setting.value):
            return jsonify({"error": "invalid bot service token"}), 401
        return f(*args, **kwargs)

    return decorated


def _check_bot_service_token(token: str) -> bool:
    import secrets as _secrets
    from app.models import SystemSetting

    setting = SystemSetting.query.filter_by(key="bot_service_token").first()
    if setting is None or not setting.value:
        return False
    return _secrets.compare_digest(token, setting.value)


def admin_or_bot_token_required(f):
    """Admin JWT OR bot service token. Used on admin endpoints the bot also needs (user CRUD, restart, stats)."""

    @wraps(f)
    def decorated(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer" or not value.strip():
            return jsonify({"message": "Token is missing!"}), 401
        token = value.strip()

        if _check_bot_service_token(token):
            return f(*args, **kwargs)

        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            return jsonify({"message": "Token is expired!"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"message": "Token is invalid!"}), 401

        if payload.get("role") != "admin":
            return jsonify({"message": "Token is invalid!"}), 401

        from app.models import Admin
        from app.extensions import db

        admin = None
        admin_id = payload.get("admin_id")
        if admin_id is not None:
            try:
                admin = db.session.get(Admin, int(admin_id))
            except (TypeError, ValueError):
                admin = None
        if admin is None and payload.get("user"):
            admin = Admin.query.filter_by(username=payload.get("user")).first()
        if admin is None:
            return jsonify({"message": "Token is invalid!"}), 401

        token_pwd_version = payload.get("pwdv")
        try:
            token_pwd_version = int(token_pwd_version)
        except (TypeError, ValueError):
            return jsonify({"message": "Token is invalid!"}), 401

        current_pwd_version = int(admin.password_changed_at or 0)
        if token_pwd_version != current_pwd_version:
            return jsonify({"message": "Token is invalid!"}), 401
        return f(*args, **kwargs)

    return decorated


def _check_federation_token(token: str) -> bool:
    import hmac
    from app.models import FederationConfig

    cfg = FederationConfig.query.get(1)
    if cfg is None or not cfg.federation_token:
        return False
    return hmac.compare_digest(token, cfg.federation_token)


def federation_token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("X-Federation-Token", "")
        if not token or not _check_federation_token(token):
            return jsonify({"error": "invalid or missing federation token"}), 401
        return f(*args, **kwargs)

    return decorated


def admin_or_federation_token_required(f):
    """Admin JWT OR bot service token OR federation token."""

    @wraps(f)
    def decorated(*args, **kwargs):
        fed_token = request.headers.get("X-Federation-Token", "")
        if fed_token and _check_federation_token(fed_token):
            return f(*args, **kwargs)
        # Fall through to existing admin_or_bot logic
        header = request.headers.get("Authorization", "")
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer" or not value.strip():
            return jsonify({"message": "Token is missing!"}), 401
        token = value.strip()
        if _check_bot_service_token(token):
            return f(*args, **kwargs)
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            return jsonify({"message": "Token is expired!"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"message": "Token is invalid!"}), 401
        if payload.get("role") != "admin":
            return jsonify({"message": "Token is invalid!"}), 401
        from app.models import Admin
        from app.extensions import db

        admin = None
        admin_id = payload.get("admin_id")
        if admin_id is not None:
            try:
                admin = db.session.get(Admin, int(admin_id))
            except (TypeError, ValueError):
                admin = None
        if admin is None and payload.get("user"):
            admin = Admin.query.filter_by(username=payload.get("user")).first()
        if admin is None:
            return jsonify({"message": "Token is invalid!"}), 401
        token_pwd_version = payload.get("pwdv")
        try:
            token_pwd_version = int(token_pwd_version)
        except (TypeError, ValueError):
            return jsonify({"message": "Token is invalid!"}), 401
        current_pwd_version = int(admin.password_changed_at or 0)
        if token_pwd_version != current_pwd_version:
            return jsonify({"message": "Token is invalid!"}), 401
        return f(*args, **kwargs)

    return decorated


def validate_password(password):
    if len(password) < 8:
        return "Password must be at least 8 characters long"
    if not all(32 <= ord(c) < 127 for c in password):
        return "Password must contain only printable ASCII characters"
    if not any(c.isupper() for c in password):
        return "Password must contain at least one uppercase letter"
    if not any(c.islower() for c in password):
        return "Password must contain at least one lowercase letter"
    if not any(c.isdigit() for c in password):
        return "Password must contain at least one digit"
    return None


def normalize_tag(value, field_name="Tag"):
    tag = str(value or "").strip()
    if not tag:
        raise ValueError(f"{field_name} required")
    if not TAG_PATTERN.fullmatch(tag):
        raise ValueError(f"{field_name} has invalid characters (allowed: A-Z a-z 0-9 . _ -)")
    return tag


def normalize_email(value, field_name="Email"):
    email = str(value or "").strip()
    if not email:
        raise ValueError(f"{field_name} required")
    if not EMAIL_PATTERN.fullmatch(email):
        raise ValueError(f"{field_name} has invalid format")
    return email


def parse_int(value, field_name, default=0, min_value=None, max_value=None):
    if value is None or value == "":
        value = default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be an integer")
    if min_value is not None and parsed < min_value:
        raise ValueError(f"{field_name} must be >= {min_value}")
    if max_value is not None and parsed > max_value:
        raise ValueError(f"{field_name} must be <= {max_value}")
    return parsed
