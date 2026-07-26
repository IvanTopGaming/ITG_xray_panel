from panel_core.app_base import build_base_app, db_path
from panel_core.panel_role import ROLE_SUB
from panel_core.xray.gateway import NullXrayGateway, set_xray_gateway, xray_gateway_configured


def create_app():
    app = build_base_app(ROLE_SUB)
    sqlite_path = db_path()

    if not xray_gateway_configured():
        set_xray_gateway(NullXrayGateway())

    from panel_core.api import subscription

    app.register_blueprint(subscription.bp, url_prefix="/api")

    app.logger.info("backend ready (db=%s, scheduler started)", sqlite_path)
    return app
