import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.bot.yml"
ROUTES_FILE = REPO_ROOT / "caddy" / "routes.yaml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")

WHY = (
    "caddygen drops an SNI route only when its ${VAR} interpolates to the EMPTY STRING "
    '(caddy/caddygen/config.go, `if r.Match == "" { continue }`). It has no notion of a host role: '
    "every variable the caddy container can see turns that route on. The bot host must publish the "
    "YooKassa webhook and nothing else, so its caddy container must be handed BOT_DOMAIN alone.\n\n"
    "Two ways to break that, both of which look harmless in a diff:\n"
    "  1. `PANEL_DOMAIN=${PANEL_DOMAIN:-}` does NOT pass an empty string. Compose's `:-` substitutes "
    "the value from .env whenever the variable is present, and defaults only when it is ABSENT. "
    "PANEL_DOMAIN is mandatory on a bot host (bot-api needs it for sub_links.build_aggregate_sub_url "
    "and federation._build_panel_url), so it is always present and always substituted non-empty.\n"
    "  2. `env_file: - .env` re-injects the whole .env into the container regardless of what the "
    "`environment:` block lists, so pruning that block alone changes nothing.\n\n"
    "The consequence is not cosmetic. SNI is chosen by the client and the box serves its certificate "
    "for whatever name is asked, so a live panel route on the bot box means "
    "https://<PANEL_DOMAIN>/<PANEL_SECRET_PATH>/api/... aimed at the BOT host's IP is answered by "
    "bot-api -- reaching /api/billing/checkout and all of /bot-service/*, which is exactly the surface "
    "`only_paths` exists to withhold. Both are token-protected, so this is defence in depth rather "
    "than an open door, but it is the phase's stated readiness criterion: no panel and no "
    "subscription route on the bot host."
)


def _service(name):
    document = yaml.safe_load(COMPOSE_FILE.read_text()) or {}
    services = document.get("services") or {}
    assert name in services, (
        f"service '{name}' is gone from {COMPOSE_FILE.name}; this guard would pass vacuously. "
        f"Found: {sorted(services)}."
    )
    return services[name]


def _environment_keys(definition):
    entries = definition.get("environment") or []
    if isinstance(entries, dict):
        return set(entries)
    return {str(entry).split("=", 1)[0] for entry in entries}


def _env_example_keys():
    keys = set()
    for line in ENV_EXAMPLE.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        keys.add(stripped.split("=", 1)[0].strip())
    assert keys, f"parsed no keys out of {ENV_EXAMPLE.name} -- the env_file arm of this guard is blind."
    return keys


def _routes():
    document = yaml.safe_load(ROUTES_FILE.read_text()) or {}
    routes = document.get("sni_routes") or []
    assert routes, f"no sni_routes in {ROUTES_FILE}; this guard would pass vacuously."
    return routes


def _caddy_visible_vars():
    definition = _service("caddy")
    visible = _environment_keys(definition)
    if definition.get("env_file"):
        visible |= _env_example_keys()
    return visible


def _selectable_route_names():
    visible = _caddy_visible_vars()
    selected = {}
    for route in _routes():
        match = str(route.get("match") or "")
        needed = set(ENV_REF.findall(match))
        if needed and needed <= visible:
            selected[route.get("name")] = sorted(needed)
    return selected


def test_the_bot_route_is_still_selected_by_bot_domain():
    names = {route.get("name") for route in _routes()}
    assert "bot" in names, (
        f"caddy/routes.yaml no longer has a route named 'bot' (found {sorted(names)}). The bot host's "
        "whole reason to run Caddy is publishing /api/billing/yookassa/webhook."
    )
    assert _selectable_route_names().get("bot") == ["BOT_DOMAIN"], (
        "the bot route is no longer selected by BOT_DOMAIN alone on the bot host: "
        f"{_selectable_route_names()}. Without it the webhook is served but unreachable, which is the "
        f"exact gap this phase closed.\n\n{WHY}"
    )


def test_the_bot_host_caddy_selects_no_route_but_the_webhook():
    selected = _selectable_route_names()
    extra = {name: vars_ for name, vars_ in selected.items() if name != "bot"}
    assert extra == {}, (
        f"docker-compose.bot.yml's caddy container also turns on these caddy/routes.yaml routes: "
        f"{extra} (route name -> the variables that enabled it). Remove those variables from the "
        f"service so the route interpolates empty and caddygen drops it.\n\n{WHY}"
    )


def test_the_bot_host_caddy_does_not_reinject_the_whole_env_file():
    definition = _service("caddy")
    assert not definition.get("env_file"), (
        "docker-compose.bot.yml's caddy service declares env_file, which hands the container every "
        "variable in .env -- PANEL_DOMAIN, PROXY_DOMAIN and SUB_DOMAIN among them, plus DATABASE_URL "
        "and SECRET_KEY, which Caddy has no business holding. Pruning the `environment:` block does "
        f"not undo this.\n\n{WHY}"
    )


def test_bot_api_still_receives_panel_domain():
    assert "PANEL_DOMAIN" in _environment_keys(_service("bot-api")), (
        "bot-api lost PANEL_DOMAIN. Unlike the caddy service it genuinely needs it: "
        "sub_links.build_aggregate_sub_url falls back to PANEL_DOMAIN + PANEL_SECRET_PATH when "
        "SUB_DOMAIN is empty, and federation._build_panel_url reads it too. The narrowing belongs to "
        "the caddy container only."
    )
