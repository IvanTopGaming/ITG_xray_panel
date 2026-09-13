def cold_state(**overrides):
    return {
        "outbounds": [],
        "routing_profiles": [],
        "balancers": [],
        "settings": [],
        "receipts": [],
        "notification_logs": [],
        "events": [],
        "entitlements": [],
        "account_access": [],
        "admin": None,
        "identity": {},
        **overrides,
    }


def full_state(snapshot):
    return {
        "hot": {"inbounds": snapshot["inbounds"]},
        "cold": cold_state(),
        "fingerprint": snapshot["cold_fingerprint"],
        "instance_id": snapshot["instance_id"],
        "timestamp": snapshot["timestamp"],
        "app_version": snapshot["app_version"],
    }
