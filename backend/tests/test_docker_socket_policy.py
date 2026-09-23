import http.server
import os
from pathlib import Path
import shutil
import socket
import socketserver
import subprocess
import threading
import time

import pytest
import requests

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def proxy(tmp_path_factory):
    binary = os.getenv("HAPROXY_BINARY") or shutil.which("haproxy")
    if not binary:
        pytest.skip("HAProxy is required for the Docker API boundary test")
    tmp = tmp_path_factory.mktemp("docker-policy")
    policy = ROOT / "scripts" / "docker-socket-proxy.cfg"
    assert policy.is_file()
    reached = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            reached.append((self.command, self.path))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        do_POST = do_GET
        do_DELETE = do_GET

        def log_message(self, *args):
            pass

    server = socketserver.UnixStreamServer(str(tmp / "docker.sock"), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = tmp / "haproxy.cfg"
    config.write_text(
        policy.read_text()
        .replace(":2375", f"127.0.0.1:{port}")
        .replace("/var/run/docker.sock", str(tmp / "docker.sock"))
    )
    process = subprocess.Popen([binary, "-db", "-f", str(config)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            if process.poll() is not None:
                pytest.fail(process.stderr.read().decode())
            try:
                requests.get(url + "/_ping", timeout=0.2)
                break
            except requests.ConnectionError:
                time.sleep(0.02)
        yield url, reached
    finally:
        process.terminate()
        process.wait(timeout=5)
        process.stderr.close()
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/version"),
        ("GET", "/v1.47/containers/xray-core/json"),
        ("GET", "/v1.47/containers/xray-core/logs?tail=10"),
        ("POST", "/v1.47/containers/xray-core/restart"),
        ("POST", "/containers/xray-egress/restart"),
    ],
)
def test_required_docker_operations_reach_daemon(proxy, method, path):
    url, reached = proxy
    assert requests.request(method, url + path, timeout=2).status_code == 200
    assert reached[-1] == (method, path)


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/containers/create"),
        ("POST", "/containers/xray-core/start"),
        ("POST", "/containers/xray-core/exec"),
        ("POST", "/containers/other/restart"),
        ("POST", "/containers/xray-core/restart/extra"),
        ("DELETE", "/containers/xray-core"),
        ("GET", "/containers/other/json"),
        ("POST", "/v1.47/containers/create"),
        ("POST", "/containers/%78ray-core/exec"),
        ("POST", "/containers/xray-core/restart%2f..%2fexec"),
    ],
)
def test_unauthorized_docker_operations_never_reach_daemon(proxy, method, path):
    url, reached = proxy
    before = len(reached)
    assert requests.request(method, url + path, timeout=2).status_code == 403
    assert len(reached) == before
