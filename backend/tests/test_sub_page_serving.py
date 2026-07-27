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
