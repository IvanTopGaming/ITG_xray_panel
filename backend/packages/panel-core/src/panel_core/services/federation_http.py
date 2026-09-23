import ipaddress
import os
import socket

import requests
from flask import has_app_context
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import NewConnectionError, ConnectTimeoutError

from panel_core.extensions import db
from panel_core.models import SystemSetting

_PRIVATE_POLICY_KEY = "federation_allow_private_urls"


def private_urls_allowed():
    value = os.getenv("FEDERATION_ALLOW_PRIVATE_URLS")
    if value is not None:
        return value.strip().lower() in ("1", "true", "yes", "on")
    if not has_app_context():
        return False
    policy = SystemSetting.query.filter_by(key=_PRIVATE_POLICY_KEY).first()
    return policy is not None and policy.value == "true"


def sync_private_network_policy():
    allowed = os.getenv("FEDERATION_ALLOW_PRIVATE_URLS", "").strip().lower() in ("1", "true", "yes", "on")
    policy = SystemSetting.query.filter_by(key=_PRIVATE_POLICY_KEY).first()
    if policy is None:
        policy = SystemSetting(key=_PRIVATE_POLICY_KEY)
        db.session.add(policy)
    policy.value = "true" if allowed else "false"
    db.session.commit()


def require_public_address(value):
    address = ipaddress.ip_address(value)
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if not address.is_global or address.is_multicast:
        raise ValueError(
            "Panel URL resolves to a non-routable address. Set FEDERATION_ALLOW_PRIVATE_URLS=true only for an intentional private topology."
        )


class _PublicConnection:
    def _new_conn(self):
        if private_urls_allowed():
            return super()._new_conn()
        addresses = socket.getaddrinfo(self._dns_host, self.port, type=socket.SOCK_STREAM)
        for entry in addresses:
            require_public_address(entry[4][0])
        original = self._dns_host
        error = None
        try:
            for entry in addresses:
                self._dns_host = entry[4][0]
                try:
                    return super()._new_conn()
                except (OSError, NewConnectionError, ConnectTimeoutError) as exc:
                    error = exc
        finally:
            self._dns_host = original
        raise error or OSError("Panel hostname resolved to no addresses")


class PublicHTTPConnection(_PublicConnection, HTTPConnection):
    pass


class PublicHTTPSConnection(_PublicConnection, HTTPSConnection):
    pass


class _PublicHTTPPool(HTTPConnectionPool):
    ConnectionCls = PublicHTTPConnection


class _PublicHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = PublicHTTPSConnection


class _FederationAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {"http": _PublicHTTPPool, "https": _PublicHTTPSPool}


def federation_session():
    session = requests.Session()
    session.trust_env = False
    session.verify = os.getenv("REQUESTS_CA_BUNDLE") or os.getenv("CURL_CA_BUNDLE") or True
    session.max_redirects = 0
    session.mount("http://", _FederationAdapter())
    session.mount("https://", _FederationAdapter())
    return session


def federation_post(url, **kwargs):
    with federation_session() as session:
        return session.post(url, **kwargs)


def federation_get(url, **kwargs):
    with federation_session() as session:
        return session.get(url, **kwargs)
