import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
ROUTES_FILE = REPO_ROOT / "caddy" / "routes.yaml"

ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")

HOSTS = {
    "master": {
        "compose": "docker-compose.master.yml",
        "example": ".env.master.example",
        "routes": {"panel"},
        "serves": "the admin panel",
    },
    "node": {
        "compose": "docker-compose.node.yml",
        "example": ".env.node.example",
        "routes": {"proxy", "panel"},
        "serves": "this node's own admin panel and its decoy SNI",
    },
    "sub": {
        "compose": "docker-compose.sub.yml",
        "example": ".env.sub.example",
        "routes": {"sub"},
        "serves": "subscription links",
    },
    "bot": {
        "compose": "docker-compose.bot.yml",
        "example": ".env.bot.example",
        "routes": {"bot"},
        "serves": "the YooKassa webhook",
    },
}

WHY = (
    "caddygen drops an SNI route only when its ${VAR} interpolates to the EMPTY STRING "
    '(caddy/caddygen/config.go, `if r.Match == "" { continue }`). It has no notion of a host role: '
    "every domain variable the caddy container can see turns one more route on, pointed at THIS box's "
    "own services.\n\n"
    "Two ways to break that, both of which look harmless in a diff:\n"
    "  1. `PANEL_DOMAIN=${PANEL_DOMAIN:-}` does NOT pass an empty string. Compose's `:-` substitutes "
    "the value from .env whenever the variable is present, and defaults only when it is ABSENT — and "
    "these variables are present on every host that needs them for its backend.\n"
    "  2. `env_file: - .env` re-injects the whole .env into the container regardless of what the "
    "`environment:` block lists, so pruning that block alone changes nothing.\n\n"
    "The consequence is not cosmetic. SNI is chosen by the client and the box serves its certificate "
    "for whatever name is asked, so a live panel route on the wrong box means "
    "https://<PANEL_DOMAIN>/<PANEL_SECRET_PATH>/api/... aimed at THAT box's IP is answered by whatever "
    "backend sits behind it. On the bot host that reaches /api/billing/checkout and all of "
    "/bot-service/*; on a node it reaches that node's admin API. Everything behind those routes is "
    "still token-protected, so this is defence in depth rather than an open door — but withholding the "
    "surface entirely is the stated point of the narrowing."
)


def _document(compose):
    path = REPO_ROOT / compose
    assert path.is_file(), f"{compose} does not exist under {REPO_ROOT}"
    return yaml.safe_load(path.read_text()) or {}


def _service(compose, name):
    services = _document(compose).get("services") or {}
    assert name in services, (
        f"service '{name}' is gone from {compose}; this guard would pass vacuously. Found: {sorted(services)}."
    )
    return services[name]


def _environment_keys(definition):
    entries = definition.get("environment") or []
    if isinstance(entries, dict):
        return set(entries)
    return {str(entry).split("=", 1)[0] for entry in entries}


def _example_keys(example):
    path = REPO_ROOT / example
    assert path.is_file(), f"{example} does not exist under {REPO_ROOT}"
    keys = set()
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        keys.add(stripped.split("=", 1)[0].strip())
    assert keys, f"parsed no keys out of {example} -- the env_file arm of this guard is blind."
    return keys


def _routes():
    document = yaml.safe_load(ROUTES_FILE.read_text()) or {}
    routes = document.get("sni_routes") or []
    assert routes, f"no sni_routes in {ROUTES_FILE}; this guard would pass vacuously."
    return routes


def _caddy_visible_vars(host):
    definition = _service(host["compose"], "caddy")
    visible = _environment_keys(definition)
    if definition.get("env_file"):
        visible |= _example_keys(host["example"])
    return visible


def _selectable_route_names(host):
    visible = _caddy_visible_vars(host)
    selected = {}
    for route in _routes():
        match = str(route.get("match") or "")
        if not match:
            continue
        needed = set(ENV_REF.findall(match))
        if not needed or needed <= visible:
            selected[route.get("name")] = sorted(needed)
    return selected


@pytest.mark.parametrize("name", sorted(HOSTS))
def test_each_host_selects_exactly_its_own_routes(name):
    host = HOSTS[name]
    declared = {route.get("name") for route in _routes()}
    assert host["routes"] <= declared, (
        f"caddy/routes.yaml no longer declares {sorted(host['routes'] - declared)}; the {name} host "
        f"exists to serve {host['serves']}."
    )
    selected = set(_selectable_route_names(host))
    assert selected == host["routes"], (
        f"{host['compose']}'s caddy container turns on {sorted(selected)}, want exactly "
        f"{sorted(host['routes'])} ({_selectable_route_names(host)} maps each selected route to the "
        f"variables that enabled it). Remove the extra variables from the service so those routes "
        f"interpolate empty and caddygen drops them.\n\n{WHY}"
    )


@pytest.mark.parametrize("name", sorted(HOSTS))
def test_no_hosts_caddy_reinjects_the_whole_env_file(name):
    host = HOSTS[name]
    assert not _service(host["compose"], "caddy").get("env_file"), (
        f"{host['compose']}'s caddy service declares env_file, which hands the container every variable "
        f"in that host's .env -- the other hosts' domains among them, plus DATABASE_URL and SECRET_KEY, "
        f"which Caddy has no business holding. Pruning the `environment:` block does not undo "
        f"this.\n\n{WHY}"
    )


def test_the_master_no_longer_demands_a_decoy_domain_it_cannot_serve():
    compose = (REPO_ROOT / "docker-compose.master.yml").read_text()
    assert "PROXY_DOMAIN" not in compose, (
        "docker-compose.master.yml still names PROXY_DOMAIN. The master has had no xray service since "
        "phase 3b, so the decoy route has no upstream: a request for the masquerade domain aimed at the "
        "master's IP is dropped instead of answered, which is precisely the opposite of what a "
        "masquerade is for. It was also a `${PROXY_DOMAIN:?}`, so the master refused to start without a "
        "value for a route it should never have had."
    )
    assert "xray" not in (_document("docker-compose.master.yml").get("services") or {}), (
        "docker-compose.master.yml grew an xray service back; the assertion above is now wrong rather "
        "than merely stale."
    )


def test_the_bot_route_allowlists_only_the_webhook_path():
    routes = {route.get("name"): route for route in _routes()}
    only_paths = routes["bot"].get("only_paths")
    assert only_paths == ["/api/billing/yookassa/webhook"], (
        f"the bot route's only_paths allowlist is {only_paths!r}, not exactly "
        '["/api/billing/yookassa/webhook"]. only_paths is the mechanism that keeps the bot host from '
        "publishing the rest of bot-api -- /bot-service/* and /api/billing/checkout -- on BOT_DOMAIN. "
        "Neither of the route-selection guards above checks what a selected route actually exposes."
    )


def test_the_sub_route_allowlists_only_the_subscription_prefix():
    routes = {route.get("name"): route for route in _routes()}
    only_paths = routes["sub"].get("only_paths")
    assert only_paths == ["/api/sub/"], (
        f'the sub route\'s only_paths allowlist is {only_paths!r}, not exactly ["/api/sub/"]. The '
        "subscription page and its assets are served under that same prefix on purpose; widening the "
        "list to '/' would match everything, because caddygen turns each entry into the glob p+'*'."
    )


def test_bot_api_still_receives_panel_domain():
    assert "PANEL_DOMAIN" in _environment_keys(_service("docker-compose.bot.yml", "bot-api")), (
        "bot-api lost PANEL_DOMAIN. Unlike the caddy service it genuinely needs it: "
        "federation._build_panel_url reads it, and services/sub_links falls back to it. The narrowing "
        "belongs to the caddy container only."
    )


def test_bot_api_receives_sub_domain_explicitly():
    definition = _service("docker-compose.bot.yml", "bot-api")
    entries = definition.get("environment") or []
    entry = next((str(item) for item in entries if str(item).startswith("SUB_DOMAIN=")), None)
    assert entry, (
        "docker-compose.bot.yml's bot-api does not list SUB_DOMAIN in its `environment:` block. bot-api "
        "builds every subscription link the bot hands a user, in its own process, out of its own "
        "environment: GET /bot-service/users/<id>/state returns sub_url from "
        "services/sub_links.build_aggregate_sub_url. Without SUB_DOMAIN that function falls back to "
        "PANEL_DOMAIN + PANEL_SECRET_PATH -- both of which this service DOES declare -- so the compose "
        "file would carry everything needed for the wrong answer and nothing for the right one. The "
        "wrong answer is a valid URL pointing at the master, which bakes no subscription-page bundle: "
        "browsers get 503, client apps still fetch configs, so it fails silently."
    )
    assert ":?" in entry, (
        f"bot-api declares SUB_DOMAIN as {entry!r}, which tolerates an empty value. An empty SUB_DOMAIN "
        "here is not a degraded mode, it is every subscription link in Telegram pointing at a host that "
        "cannot render the page. Require it: ${SUB_DOMAIN:?SUB_DOMAIN is required}."
    )


@pytest.mark.parametrize("name", ["master", "node"])
def test_the_panel_backends_declare_the_secret_path_explicitly(name):
    definition = _service(HOSTS[name]["compose"], "backend")
    assert "PANEL_SECRET_PATH" in _environment_keys(definition), (
        f"{HOSTS[name]['compose']}'s backend does not list PANEL_SECRET_PATH in `environment:`. It reads "
        f"it -- services/sub_links.build_aggregate_sub_url uses it for the fallback subscription URL, "
        f"and on the node api/federation.py builds the panel URL from it -- so leaving it to arrive "
        f"through `env_file` alone is the same trap SUB_DOMAIN was on bot-api: the variable is "
        f"load-bearing while being invisible in the compose file."
    )


@pytest.mark.parametrize("name", ["master", "node"])
def test_the_frontend_containers_do_not_receive_the_whole_env_file(name):
    definition = _service(HOSTS[name]["compose"], "frontend")
    assert not definition.get("env_file"), (
        f"{HOSTS[name]['compose']}'s frontend service declares env_file. frontend/entrypoint.sh reads "
        f"exactly two variables, PANEL_SECRET_PATH and PANEL_ROLE (its envsubst call is restricted to "
        f"the first), and both are already listed explicitly -- so env_file only hands an nginx "
        f"container SECRET_KEY, DATABASE_URL, PANEL_ADMIN_PASSWORD and the Redis credentials for no "
        f"reason at all."
    )
    assert _environment_keys(definition) >= {"PANEL_SECRET_PATH", "PANEL_ROLE"}, (
        f"{HOSTS[name]['compose']}'s frontend no longer declares both variables its entrypoint needs; "
        f"with env_file gone there is nothing else to deliver them."
    )
