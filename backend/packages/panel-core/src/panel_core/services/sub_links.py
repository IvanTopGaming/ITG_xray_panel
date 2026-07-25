import os


def build_aggregate_sub_url(token):

    if not token:
        return None
    sub_domain = os.getenv("SUB_DOMAIN", "").strip()
    if sub_domain:
        return f"https://{sub_domain}/api/sub/u/{token}"
    panel = os.getenv("PANEL_DOMAIN", "").strip()
    if not panel:
        return None
    secret = os.getenv("PANEL_SECRET_PATH", "").strip("/")
    base = f"https://{panel}/{secret}" if secret else f"https://{panel}"
    return f"{base}/api/sub/u/{token}"
