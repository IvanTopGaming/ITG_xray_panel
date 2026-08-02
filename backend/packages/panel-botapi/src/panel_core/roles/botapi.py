from panel_core.app_base import build_base_app, db_path, ensure_scheduler_job, start_scheduler
from panel_core.jobs.payments import cleanup_old_payments, poll_pending_payments, reconcile_refunds
from panel_core.panel_role import ROLE_BOT
from panel_core.xray.gateway import NullXrayGateway, set_xray_gateway, xray_gateway_configured


def create_app():
    app = build_base_app(ROLE_BOT)
    sqlite_path = db_path()

    if not xray_gateway_configured():
        set_xray_gateway(NullXrayGateway())

    ensure_scheduler_job("poll_pending_payments", poll_pending_payments, 30)
    ensure_scheduler_job("reconcile_refunds", reconcile_refunds, 3600)
    ensure_scheduler_job("cleanup_old_payments", cleanup_old_payments, 86400)
    start_scheduler()

    from panel_core.api import bot_service, billing

    app.register_blueprint(bot_service.bp, url_prefix="/api")
    app.register_blueprint(billing.bp, url_prefix="/api")

    app.logger.info("backend ready (db=%s, scheduler started)", sqlite_path)
    return app
