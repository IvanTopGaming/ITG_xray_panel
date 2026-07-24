import os


def is_worker():
    return (os.getenv("PANEL_ROLE", "") or "").strip().lower() == "worker"
