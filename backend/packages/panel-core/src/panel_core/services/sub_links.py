import os


def sub_base_url():

    sub_domain = os.getenv("SUB_DOMAIN", "").strip()
    if not sub_domain:
        return None
    return f"https://{sub_domain}"


def build_aggregate_sub_url(token):

    if not token:
        return None
    base = sub_base_url()
    if not base:
        return None
    return f"{base}/api/sub/u/{token}"


def build_client_sub_url(client_id):

    if not client_id:
        return None
    base = sub_base_url()
    if not base:
        return None
    return f"{base}/api/sub/{client_id}"
