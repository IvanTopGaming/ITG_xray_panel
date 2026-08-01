import os

ROLE_ENV = "PANEL_ROLE"

ROLE_MASTER = "master"
ROLE_WORKER = "worker"
ROLE_SUB = "sub"
ROLE_BOT = "bot"
ROLE_CRON = "cron"

NAMED_ROLES = (ROLE_WORKER, ROLE_SUB, ROLE_BOT, ROLE_CRON)
KNOWN_ROLES = NAMED_ROLES + (ROLE_MASTER,)


def normalize_role(raw):
    """Unset means master. A value that is set but unrecognised is an error, not a master.

    The two cases used to collapse into one: anything the tuple did not contain became `master`,
    the most privileged role there is. Unset genuinely does mean master and is documented that way,
    but `PANEL_ROLE=worke` meant the same thing — silently, with the admin bootstrap that comes
    with a master. In a container the gunicorn command decides the role and `bind_role` would catch
    the contradiction, so this bites exactly where nothing else is watching: the dev entry point,
    where the env variable really does choose.
    """

    value = (raw or "").strip().lower()
    if not value:
        return ROLE_MASTER
    if value not in KNOWN_ROLES:
        raise ValueError(
            f"{ROLE_ENV}={raw!r} is not a role. Expected one of {KNOWN_ROLES}, or leave it unset for "
            f"{ROLE_MASTER!r}. It used to fall through to {ROLE_MASTER!r}, which is the one role you "
            f"least want a typo to select."
        )
    return value


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


def is_cron():
    return current_role() == ROLE_CRON
