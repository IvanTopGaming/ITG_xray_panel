import ipaddress
import os
import socket

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import NewConnectionError, ConnectTimeoutError


def private_urls_allowed():
    return os.getenv("FEDERATION_ALLOW_PRIVATE_URLS", "").strip().lower() in ("1", "true", "yes", "on")


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
