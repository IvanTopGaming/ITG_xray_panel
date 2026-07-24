import os


def is_worker():
    return (os.getenv("PANEL_ROLE", "") or "").strip().lower() == "worker"


def is_sub():
    return (os.getenv("PANEL_ROLE", "") or "").strip().lower() == "sub"
