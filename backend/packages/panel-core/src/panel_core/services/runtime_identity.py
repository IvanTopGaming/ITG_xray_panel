import base64
import binascii

RUNTIME_EMAIL_PREFIX = "v1"
RUNTIME_EMAIL_SEPARATOR = "|"


def _encode_email(email):
    raw = str(email or "").encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_email(value):
    encoded = str(value or "").strip()
    if not encoded:
        return ""
    padded = encoded + ("=" * ((4 - len(encoded) % 4) % 4))
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return ""


def build_runtime_email(inbound_tag, email):
    inbound = str(inbound_tag or "").strip()
    value = str(email or "").strip()
    if not inbound or not value:
        return value
    return f"{RUNTIME_EMAIL_PREFIX}{RUNTIME_EMAIL_SEPARATOR}{inbound}{RUNTIME_EMAIL_SEPARATOR}{_encode_email(value)}"


def parse_runtime_email(value):
    raw = str(value or "").strip()
    if not raw:
        return "", ""
    parts = raw.split(RUNTIME_EMAIL_SEPARATOR, 2)
    if len(parts) == 3 and parts[0] == RUNTIME_EMAIL_PREFIX:
        decoded = _decode_email(parts[2])
        if decoded:
            return parts[1], decoded
    return "", raw
