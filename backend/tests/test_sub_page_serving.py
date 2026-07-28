import os
import pathlib
import re

import pytest

from panel_core.extensions import db
from panel_core.models import TelegramUser


BROWSER_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
CLIENT_UA = "v2rayNG/1.8.6"

SERVING_DOC = (
    "The sub service is the one opened when everything else is already broken, so a missing UI bundle "
    "must never take the whole role down with it: config delivery to client apps is the critical "
    "function and stays alive, while the page and its assets answer 503. The asset route also has to "
    "out-rank both /sub/u/<token> and /sub/<path:uuid_str>; Werkzeug ranks by static-segment count "
    "rather than declaration order, and a subscription token that happened to be the string 'assets' "
    "would otherwise shadow the whole bundle."
)

REPO = pathlib.Path(__file__).resolve().parents[2]

TOPOLOGY_DOC = (
    "One image bakes the page bundle, three roles register the subscription blueprint. That asymmetry "
    "is the whole reason SUB_DOMAIN stopped being optional: with it empty, build_aggregate_sub_url "
    "falls back to PANEL_DOMAIN, caddy/routes.yaml sends that to the master, and the master has no "
    "bundle — so the browser branch 503s where a rendered page used to be, while client apps keep "
    "getting configs and nobody notices. It is a deployment-topology rule, enforced by nothing at "
    "runtime, so CLAUDE.md carrying it is the only thing standing between a deployer and that trap. "
    "This guard fails when the topology changes without the prose, or the prose without the topology."
)

ROLES_SERVING_THE_SUBSCRIPTION_ROUTES = {"master", "worker", "sub"}

TOPOLOGY_CLAIMS = {
    "the Subscription links section": "**Only the `sub` role serves the subscription page.**",
    "the Configuration SUB_DOMAIN bullet": "`SUB_DOMAIN` *(required for the subscription page)*",
    "the Phase 3d fan-out rule": "**Phase 6 is a standing exception to the three-image fan-out above",
}


def _roles_registering_the_subscription_blueprint():
    found = set()
    for role_file in sorted((REPO / "backend" / "packages").glob("*/src/panel_core/roles/*.py")):
        if "register_blueprint(subscription.bp" in role_file.read_text():
            found.add(role_file.stem)
    return found


def _dockerfiles_baking_a_frontend_bundle():
    found = set()
    for dockerfile in sorted((REPO / "backend").glob("Dockerfile*")):
        text = dockerfile.read_text()
        if "npm run build" in text or "sub-page/dist" in text:
            found.add(dockerfile.name)
    return found


def test_exactly_one_image_bakes_the_bundle_that_three_roles_serve_routes_for():
    roles = _roles_registering_the_subscription_blueprint()
    bakers = _dockerfiles_baking_a_frontend_bundle()

    assert roles == ROLES_SERVING_THE_SUBSCRIPTION_ROUTES, (
        f"{sorted(roles)} register the subscription blueprint, not "
        f"{sorted(ROLES_SERVING_THE_SUBSCRIPTION_ROUTES)}. If master or worker dropped it, they no "
        f"longer 503 a browser and the SUB_DOMAIN-is-required rule is obsolete — rewrite the CLAUDE.md "
        f"passages this guard pins instead of leaving them to mislead. If a fourth role gained it, that "
        f"role has just inherited the same bundle-less page.\n\n{TOPOLOGY_DOC}"
    )
    assert bakers == {"Dockerfile.sub"}, (
        f"{sorted(bakers)} bake a frontend bundle, not just Dockerfile.sub. If a second image now "
        f"carries one, the roles it covers no longer 503 in a browser and the documented rule has to "
        f"change with it.\n\n{TOPOLOGY_DOC}"
    )

    claude_md = (REPO / "CLAUDE.md").read_text()
    for where, claim in TOPOLOGY_CLAIMS.items():
        assert claim in claude_md, (
            f"CLAUDE.md no longer states, in {where}: {claim!r}. The topology it describes is still "
            f"live — {sorted(roles)} register the blueprint and only {sorted(bakers)} bakes a bundle — "
            f"so deleting the prose does not delete the trap, it only hides it.\n\n{TOPOLOGY_DOC}"
        )


HOST_EXAMPLES_SETTING_SUB_DOMAIN = [
    ".env.master.example",
    ".env.node.example",
    ".env.sub.example",
    ".env.bot.example",
]


@pytest.mark.parametrize("example", HOST_EXAMPLES_SETTING_SUB_DOMAIN)
def test_no_host_example_calls_sub_domain_optional(example):
    path = REPO / example
    assert path.is_file(), f"{example} does not exist; the single .env.example was split per host."
    lines = path.read_text().splitlines()
    assert any(line.startswith("SUB_DOMAIN=") for line in lines), (
        f"{example} no longer sets SUB_DOMAIN. Every one of these four hosts reads it: the sub host "
        f"serves it, and the master, the node and bot-api all build subscription links out of it via "
        f"services/sub_links.build_aggregate_sub_url.\n\n{TOPOLOGY_DOC}"
    )
    documentation = [line for line in lines if line.startswith("# SUB_DOMAIN")]
    assert documentation, f"{example} sets SUB_DOMAIN without documenting it at all\n\n{TOPOLOGY_DOC}"
    assert "optional" not in documentation[0].lower(), (
        f"{example} calls SUB_DOMAIN optional: {documentation[0]!r}. It is optional only for config "
        f"delivery; for the subscription page it is the difference between a page and a "
        f"503.\n\n{TOPOLOGY_DOC}"
    )


@pytest.fixture
def app(app):
    from panel_core.api import subscription as sub_api

    if "subscription" not in app.blueprints:
        app.register_blueprint(sub_api.bp, url_prefix="/api")
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    dist = tmp_path / "ui"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>sub</title><div id=root></div>")
    (dist / "assets" / "index-abc123.js").write_text("console.log('bundle')")
    monkeypatch.setenv("SUB_PAGE_DIST", str(dist))
    return dist


@pytest.fixture
def sub_user(app):
    with app.app_context():
        db.session.add(TelegramUser(telegram_id=555, sub_token="tok555", language="ru"))
        db.session.commit()
    return "tok555"


def test_a_browser_gets_the_bundle_shell(app, client, bundle, sub_user):
    response = client.get(f"/api/sub/u/{sub_user}", headers={"User-Agent": BROWSER_UA})

    assert response.status_code == 200
    assert response.mimetype == "text/html"
    assert b"<div id=root>" in response.data


def test_a_client_app_still_gets_a_raw_config(app, client, bundle, sub_user):
    response = client.get(f"/api/sub/u/{sub_user}", headers={"User-Agent": CLIENT_UA})

    assert response.status_code in (200, 404)
    assert b"<div id=root>" not in response.data


def test_assets_are_served_from_the_bundle(app, client, bundle, sub_user):
    response = client.get("/api/sub/u/assets/index-abc123.js")

    assert response.status_code == 200
    assert b"console.log('bundle')" in response.data


def test_the_asset_route_outranks_the_token_route(app, client, bundle):
    with app.app_context():
        db.session.add(TelegramUser(telegram_id=556, sub_token="assets", language="ru"))
        db.session.commit()

    response = client.get("/api/sub/u/assets/index-abc123.js")

    assert response.status_code == 200, SERVING_DOC
    assert b"console.log('bundle')" in response.data


def test_a_traversal_attempt_through_the_asset_route_is_refused(app, client, bundle, sub_user):
    response = client.get("/api/sub/u/assets/..%2f..%2fetc%2fpasswd")

    assert response.status_code in (400, 404)


def test_a_missing_bundle_503s_the_page_but_keeps_config_delivery_alive(app, client, sub_user, monkeypatch, tmp_path):
    monkeypatch.setenv("SUB_PAGE_DIST", str(tmp_path / "absent"))

    page = client.get(f"/api/sub/u/{sub_user}", headers={"User-Agent": BROWSER_UA})
    assert page.status_code == 503, SERVING_DOC

    asset = client.get("/api/sub/u/assets/index-abc123.js")
    assert asset.status_code == 503, SERVING_DOC

    config = client.get(f"/api/sub/u/{sub_user}", headers={"User-Agent": CLIENT_UA})
    assert config.status_code != 503, SERVING_DOC


def test_the_dist_path_is_read_per_call_not_at_import(monkeypatch):
    from panel_core.api.subscription import sub_page_dist

    monkeypatch.setenv("SUB_PAGE_DIST", "/somewhere/else")
    assert sub_page_dist() == "/somewhere/else"
    monkeypatch.delenv("SUB_PAGE_DIST", raising=False)
    assert sub_page_dist() == os.path.join("/app", "ui")


def test_the_sub_image_bakes_the_bundle_where_the_default_dist_path_looks_for_it(monkeypatch):
    from panel_core.api.subscription import sub_page_dist

    monkeypatch.delenv("SUB_PAGE_DIST", raising=False)
    default = sub_page_dist()

    dockerfile = (pathlib.Path(__file__).resolve().parents[1] / "Dockerfile.sub").read_text()
    match = re.search(r"^COPY --from=ui \S+ (\S+)\s*$", dockerfile, re.M)

    assert match, (
        "backend/Dockerfile.sub no longer copies the built bundle out of its 'ui' stage. Nothing else "
        f"puts a bundle in the image, so the page would 503 in every container.\n\n{SERVING_DOC}"
    )
    assert match.group(1) == default, (
        f"backend/Dockerfile.sub bakes the bundle into {match.group(1)} but Flask looks for it in "
        f"{default} when SUB_PAGE_DIST is unset — which is exactly how docker-compose.sub.yml runs it. "
        f"The two are set in different languages in different files and only agree by convention; when "
        f"they disagree the role still boots and still serves configs, and only the page is dead."
        f"\n\n{SERVING_DOC}"
    )
