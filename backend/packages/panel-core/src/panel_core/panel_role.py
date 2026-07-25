import os

ROLE_ENV = "PANEL_ROLE"

ROLE_MASTER = "master"
ROLE_WORKER = "worker"
ROLE_SUB = "sub"
ROLE_BOT = "bot"

NAMED_ROLES = (ROLE_WORKER, ROLE_SUB, ROLE_BOT)
KNOWN_ROLES = NAMED_ROLES + (ROLE_MASTER,)


def normalize_role(raw):
    value = (raw or "").strip().lower()
    return value if value in NAMED_ROLES else ROLE_MASTER


def declared_role():
    return (os.getenv(ROLE_ENV, "") or "").strip()


def current_role():
    return normalize_role(declared_role())


def bind_role(role):
    if role not in KNOWN_ROLES:
        raise ValueError(f"unknown panel role {role!r}; expected one of {KNOWN_ROLES}")

    declared = declared_role()
    if declared and normalize_role(declared) != role:
        raise RuntimeError(
            f"{ROLE_ENV}={declared!r} contradicts the app factory that is running (role={role!r}). "
            f"The runtime role is fixed by the gunicorn entrypoint (panel_core.roles.<module>:create_app); "
            f"set {ROLE_ENV}={role} or leave it unset."
        )

    os.environ[ROLE_ENV] = role
    return role


def is_worker():
    return current_role() == ROLE_WORKER


def is_sub():
    return current_role() == ROLE_SUB


def is_bot_api():
    return current_role() == ROLE_BOT
