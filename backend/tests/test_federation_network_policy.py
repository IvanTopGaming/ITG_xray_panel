import base64
import socket
from unittest.mock import Mock

import pytest

from panel_core.services.master_client import decode_transfer_token


@pytest.mark.parametrize(
    "url",
    [
        "http://master.example/path",
        "https://user:pass@master.example",
        "https://master.example/path?secret=x",
        "https://master.example/#frag",
    ],
)
def test_transfer_rejects_unsafe_master_address(url):
    token = base64.urlsafe_b64encode(f"{url}|secret".encode()).decode()
    with pytest.raises(ValueError):
        decode_transfer_token(token)


def test_transfer_preserves_https_secret_path():
    token = base64.urlsafe_b64encode(b"https://master.example/secret/|token").decode()
    assert decode_transfer_token(token) == ("https://master.example/secret", "token")


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.1.2.3", "169.254.169.254", "100.64.0.1", "0.0.0.0", "224.0.0.1", "::1", "::ffff:127.0.0.1"],
)
def test_connection_revalidates_dns_before_connecting(monkeypatch, address):
    from panel_core.services.federation_http import PublicHTTPConnection

    monkeypatch.delenv("FEDERATION_ALLOW_PRIVATE_URLS", raising=False)
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 80))]
    )
    connect = Mock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(socket, "socket", connect)
    with pytest.raises(ValueError, match="FEDERATION_ALLOW_PRIVATE_URLS"):
        PublicHTTPConnection("node.example", port=80)._new_conn()
    connect.assert_not_called()


def test_public_connection_pins_resolved_ip_and_preserves_hostname(monkeypatch):
    from panel_core.services.federation_http import PublicHTTPSConnection
    from urllib3.connection import HTTPConnection

    monkeypatch.delenv("FEDERATION_ALLOW_PRIVATE_URLS", raising=False)
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
    )
    connected = []
    sentinel = object()

    def connect(connection):
        connected.append(connection.host)
        return sentinel

    monkeypatch.setattr(HTTPConnection, "_new_conn", connect)
    connection = PublicHTTPSConnection("node.example", port=443)
    assert connection._new_conn() is sentinel
    assert connected == ["8.8.8.8"]
    assert connection.host == "node.example"


def test_federation_session_enforces_policy_and_preserves_private_opt_in(monkeypatch):
    import http.server
    import threading
    from panel_core.services.federation_http import federation_session

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        monkeypatch.delenv("FEDERATION_ALLOW_PRIVATE_URLS", raising=False)
        with federation_session() as session:
            with pytest.raises(ValueError, match="FEDERATION_ALLOW_PRIVATE_URLS"):
                session.get(url, timeout=2)
        monkeypatch.setenv("FEDERATION_ALLOW_PRIVATE_URLS", "true")
        with federation_session() as session:
            assert session.get(url, timeout=2).json() == {}
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_federation_session_preserves_custom_ca_without_environment_proxy(monkeypatch):
    from panel_core.services.federation_http import federation_session

    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/configured/ca.pem")
    with federation_session() as session:
        assert session.verify == "/configured/ca.pem"
        assert not session.trust_env


def test_master_private_network_opt_in_is_shared_with_other_roles(db, monkeypatch):
    from panel_core.services.federation_http import private_urls_allowed, sync_private_network_policy

    monkeypatch.setenv("FEDERATION_ALLOW_PRIVATE_URLS", "true")
    sync_private_network_policy()
    monkeypatch.delenv("FEDERATION_ALLOW_PRIVATE_URLS")
    assert private_urls_allowed()
    monkeypatch.setenv("FEDERATION_ALLOW_PRIVATE_URLS", "false")
    sync_private_network_policy()
    monkeypatch.delenv("FEDERATION_ALLOW_PRIVATE_URLS")
    assert not private_urls_allowed()
