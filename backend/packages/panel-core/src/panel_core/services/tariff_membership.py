def client_tariff_sources(client):
    sources = client.get("tariff_sources")
    if sources:
        return sources
    if client.get("tariff_id") is None:
        return []
    return [
        {
            "tariff_id": client["tariff_id"],
            "expires_at_ms": client.get("expiry_time"),
            "enabled": client.get("enable", False),
            "revoked": False,
        }
    ]


def active_tariff_ids(client, now_ms):
    if not client.get("enable"):
        return set()
    return {
        source["tariff_id"]
        for source in client_tariff_sources(client)
        if source.get("tariff_id") is not None
        and source.get("enabled")
        and not source.get("revoked")
        and source.get("expires_at_ms") is not None
        and (source["expires_at_ms"] == 0 or source["expires_at_ms"] > now_ms)
    }
