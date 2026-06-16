import os

import requests


class MetricsClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()

    def get(self, path: str, params: dict | None = None) -> dict:
        resp = self._session.get(f"{self.base_url}{path}", params=params, timeout=(2, 5))
        resp.raise_for_status()
        return resp.json()


def default_client() -> MetricsClient:
    return MetricsClient(os.environ.get("METRICS_AGENT_URL", "http://metrics:9100"))
