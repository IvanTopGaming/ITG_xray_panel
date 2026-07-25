import os

import requests
from flask import request


def proxy_to_admin(path):
    base = (os.getenv("ADMIN_BACKEND_URL", "") or "").strip().rstrip("/")
    if not base:
        return {"error": "provisioning temporarily unavailable"}, 503
    headers = {}
    auth = request.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth
    try:
        resp = requests.request(
            method=request.method,
            url=base + path,
            json=request.get_json(silent=True),
            headers=headers,
            timeout=(3, 15),
        )
    except requests.RequestException:
        return {"error": "provisioning temporarily unavailable"}, 503
    try:
        return resp.json(), resp.status_code
    except ValueError:
        return {"error": "provisioning temporarily unavailable"}, 503
