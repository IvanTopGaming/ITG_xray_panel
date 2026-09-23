import base64
import logging
from urllib.parse import urlsplit

import requests

CLAIMED_SETTING_KEY = "node_transfer_claimed"
TRANSFER_TOKEN_ENV = "NODE_TRANSFER_TOKEN"

logger = logging.getLogger(__name__)


def decode_transfer_token(raw: str) -> tuple[str, str]:
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("transfer token is empty")
    decoded = base64.urlsafe_b64decode(raw + "==").decode()
    url, _, secret = decoded.partition("|")
    if not url or not secret:
        raise ValueError("transfer token does not carry a master URL")
    _validate_master_url(url)
    return url.rstrip("/"), secret


def _validate_master_url(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Transfer master URL must use HTTPS without credentials, query or fragment")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("Transfer master URL has an invalid port")


class MasterClient:
    def __init__(self, master_url: str) -> None:
        _validate_master_url(master_url)
        self.base_url = master_url.rstrip("/")
        self._session = requests.Session()
        self._session.max_redirects = 0

    def _post(self, path: str, payload: dict, timeout):
        resp = self._session.post(f"{self.base_url}{path}", json=payload, timeout=timeout)
        if resp.status_code >= 400:
            body = {}
            try:
                parsed = resp.json()
                if isinstance(parsed, dict):
                    body = parsed
            except Exception:
                body = {}
            raise RuntimeError(f"master answered HTTP {resp.status_code}: {body.get('error') or '(no message)'}")
        return resp.json()

    def claim(self, secret: str, instance_id: str, federation_token: str) -> dict:
        return self._post(
            "/api/panels/transfer/claim",
            {"transfer_token": secret, "instance_id": instance_id, "federation_token": federation_token},
            (3, 30),
        )

    def instance_check(self, federation_token: str, instance_id: str) -> dict:
        return self._post(
            "/api/panels/transfer/instance-check",
            {"federation_token": federation_token, "instance_id": instance_id},
            (3, 8),
        )
