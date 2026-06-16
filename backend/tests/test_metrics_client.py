from unittest.mock import patch, MagicMock

from app.services.metrics_client import MetricsClient


def test_get_sends_bearer_and_parses_json():
    c = MetricsClient("http://metrics:9100")
    fake = MagicMock()
    fake.json.return_value = {"points": [1, 2]}
    fake.raise_for_status.return_value = None
    with patch.object(c._session, "get", return_value=fake) as g:
        out = c.get("/api/v1/series", params={"metric": "cpu_host"})
    assert out == {"points": [1, 2]}
    args, kwargs = g.call_args
    assert args[0] == "http://metrics:9100/api/v1/series"
    assert kwargs["params"] == {"metric": "cpu_host"}
