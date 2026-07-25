from grpc.experimental import gevent as grpc_gevent

from panel_core.app_base import (
    bootstrap_defaults,
    build_base_app,
    db_path,
    ensure_scheduler_job,
    start_scheduler,
)
from panel_core.jobs.billing import auto_renew_free_users
from panel_core.jobs.notifications import cleanup_bot_events, replay_undelivered_bot_events
from panel_core.jobs.panels import poll_linked_panels
from panel_core.jobs.payments import cleanup_old_payments, poll_pending_payments, reconcile_refunds
from panel_core.panel_role import ROLE_MASTER
from panel_core.services.stats import (
    check_limits_job,
    cleanup_stats_job,
    parse_access_logs,
    sync_traffic_job,
)
from panel_core.services.version_check import fetch_latest
from panel_core.xray.gateway import LocalXrayGateway, set_xray_gateway, xray_gateway_configured

grpc_gevent.init_gevent()


def create_app():
    app = build_base_app(ROLE_MASTER)
    sqlite_path = db_path()

    if not xray_gateway_configured():
        set_xray_gateway(LocalXrayGateway())

    ensure_scheduler_job("sync_traffic", sync_traffic_job, 10)
    ensure_scheduler_job("check_limits", check_limits_job, 60)
    ensure_scheduler_job("parse_logs", parse_access_logs, 15)
    ensure_scheduler_job("cleanup_stats", cleanup_stats_job, 86400)
    ensure_scheduler_job("auto_renew_free_users", auto_renew_free_users, 900)
    ensure_scheduler_job("poll_pending_payments", poll_pending_payments, 30)
    ensure_scheduler_job("reconcile_refunds", reconcile_refunds, 3600)
    ensure_scheduler_job("cleanup_old_payments", cleanup_old_payments, 86400)
    ensure_scheduler_job("cleanup_bot_events", cleanup_bot_events, 86400)
    ensure_scheduler_job("replay_undelivered_bot_events", replay_undelivered_bot_events, 60)
    ensure_scheduler_job("poll_linked_panels", poll_linked_panels, 10)
    ensure_scheduler_job("check_latest_version", fetch_latest, 21600)
    start_scheduler()

    try:
        import gevent

        gevent.spawn(fetch_latest)
    except Exception:
        pass

    from panel_core.api import (
        auth,
        inbound,
        outbound,
        routing,
        system,
        subscription,
        statistics,
        bot_admin,
        bot_service,
        billing,
        panels,
        federation,
    )

    app.register_blueprint(auth.bp, url_prefix="/api")
    app.register_blueprint(inbound.bp, url_prefix="/api")
    app.register_blueprint(outbound.bp, url_prefix="/api")
    app.register_blueprint(routing.bp, url_prefix="/api")
    app.register_blueprint(system.bp, url_prefix="/api")
    app.register_blueprint(subscription.bp, url_prefix="/api")
    app.register_blueprint(statistics.bp, url_prefix="/api")
    app.register_blueprint(bot_admin.bp, url_prefix="/api")
    app.register_blueprint(bot_service.bp, url_prefix="/api")
    app.register_blueprint(billing.bp, url_prefix="/api")
    app.register_blueprint(panels.bp, url_prefix="/api")
    app.register_blueprint(federation.bp, url_prefix="/api")

    bootstrap_defaults(app, sqlite_path)

    app.logger.info("backend ready (db=%s, scheduler started)", sqlite_path)
    return app
