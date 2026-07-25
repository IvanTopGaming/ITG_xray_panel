from unittest.mock import AsyncMock, MagicMock, patch

from backend_client import BackendClient


def _response(payload):
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock()
    return resp


async def test_claim_notification_posts_expected_body():
    client = BackendClient(base_url="http://bot-api:5000/api", token="t")
    http = MagicMock()
    http.post = AsyncMock(return_value=_response({"claimed": True, "lang": "en", "renewable": True}))

    with patch.object(BackendClient, "_ensure_client", return_value=http):
        result = await client.claim_notification(telegram_id=42, kind="traffic_80", tariff_id=7, scope="de1/vless/tg42")

    assert result == {"claimed": True, "lang": "en", "renewable": True}
    (path,) = http.post.call_args.args
    assert path == "/bot-service/notifications/claim"
    assert http.post.call_args.kwargs["json"] == {
        "telegram_id": 42,
        "kind": "traffic_80",
        "tariff_id": 7,
        "scope": "de1/vless/tg42",
    }
