import os

from panel_core.app_base import bootstrap_defaults, build_base_app, db_path
from panel_core.panel_role import ROLE_MASTER
from panel_core.xray.gateway import RemoteXrayGateway, set_xray_gateway, xray_gateway_configured


def create_app():
    app = build_base_app(ROLE_MASTER)
    sqlite_path = db_path()

    if not xray_gateway_configured():
        set_xray_gateway(RemoteXrayGateway())

    from panel_core.api import (
        auth,
        inbound,
        outbound,
        routing,
        system,
        subscription,
        statistics,
        bot_admin,
        panels,
    )

    app.register_blueprint(auth.bp, url_prefix="/api")
    app.register_blueprint(inbound.bp, url_prefix="/api")
    app.register_blueprint(outbound.bp, url_prefix="/api")
    app.register_blueprint(routing.bp, url_prefix="/api")
    app.register_blueprint(system.bp, url_prefix="/api")
    app.register_blueprint(subscription.bp, url_prefix="/api")
    app.register_blueprint(statistics.bp, url_prefix="/api")
    app.register_blueprint(bot_admin.bp, url_prefix="/api")
    app.register_blueprint(panels.bp, url_prefix="/api")

    bootstrap_defaults(app)

    if not os.path.isfile(subscription.sub_page_index_path()):
        app.logger.info(
            "subscription page bundle is absent (expected on this role) — /api/sub/u/<token> answers 503 to a "
            "browser and serves configs to client apps as usual. Set SUB_DOMAIN so links point at the sub host."
        )

    app.logger.info("backend ready (db=%s, no scheduled jobs on this role)", sqlite_path)
    return app
