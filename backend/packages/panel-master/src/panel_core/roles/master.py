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
        statistics,
        bot_admin,
        panels,
    )

    app.register_blueprint(auth.bp, url_prefix="/api")
    app.register_blueprint(inbound.bp, url_prefix="/api")
    app.register_blueprint(outbound.bp, url_prefix="/api")
    app.register_blueprint(routing.bp, url_prefix="/api")
    app.register_blueprint(system.bp, url_prefix="/api")
    app.register_blueprint(statistics.bp, url_prefix="/api")
    app.register_blueprint(bot_admin.bp, url_prefix="/api")
    app.register_blueprint(panels.bp, url_prefix="/api")

    bootstrap_defaults(app)

    app.logger.info("backend ready (db=%s, no scheduled jobs on this role)", sqlite_path)
    return app
