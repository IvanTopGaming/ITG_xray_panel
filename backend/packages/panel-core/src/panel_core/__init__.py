import gevent.monkey

gevent.monkey.patch_all()

from panel_core.pg_compat import patch_gevent_psycopg

patch_gevent_psycopg()

from grpc.experimental import gevent as grpc_gevent

from .app_base import (
    bootstrap_defaults,
    build_base_app,
    db_path,
    ensure_scheduler_job,
    start_scheduler,
)
from .xray.gateway import LocalXrayGateway, set_xray_gateway, xray_gateway_configured
from .services.stats import sync_traffic_job, check_limits_job, parse_access_logs, cleanup_stats_job
from .jobs.billing import auto_renew_free_users
from .jobs.notifications import (
    cleanup_bot_events,
    replay_undelivered_bot_events,
)
from .jobs.payments import cleanup_old_payments, poll_pending_payments, reconcile_refunds
from .jobs.panels import poll_linked_panels
from .services.version_check import fetch_latest
from .panel_role import is_worker, is_sub, is_bot_api

grpc_gevent.init_gevent()


def create_app():
    if is_sub():
        from panel_core.roles import sub

        return sub.create_app()
    if is_bot_api():
        from panel_core.roles import botapi

        return botapi.create_app()

    app = build_base_app()
    sqlite_path = db_path()

    if not xray_gateway_configured():
        set_xray_gateway(LocalXrayGateway())

    if not is_sub() and not is_bot_api():
        ensure_scheduler_job("sync_traffic", sync_traffic_job, 10)
        ensure_scheduler_job("check_limits", check_limits_job, 60)
        ensure_scheduler_job("parse_logs", parse_access_logs, 15)
        ensure_scheduler_job("cleanup_stats", cleanup_stats_job, 86400)
        if not is_worker():
            ensure_scheduler_job("auto_renew_free_users", auto_renew_free_users, 900)
            ensure_scheduler_job("poll_pending_payments", poll_pending_payments, 30)
            ensure_scheduler_job("reconcile_refunds", reconcile_refunds, 3600)
            ensure_scheduler_job("cleanup_old_payments", cleanup_old_payments, 86400)
            ensure_scheduler_job("cleanup_bot_events", cleanup_bot_events, 86400)
            ensure_scheduler_job("replay_undelivered_bot_events", replay_undelivered_bot_events, 60)
            ensure_scheduler_job("poll_linked_panels", poll_linked_panels, 10)
            ensure_scheduler_job("check_latest_version", fetch_latest, 21600)
        start_scheduler()

        if not is_worker():
            try:
                import gevent

                gevent.spawn(fetch_latest)
            except Exception:
                pass

    from .api import (
        auth,
        inbound,
        outbound,
        routing,
        system,
        subscription,
        statistics,
        bot_admin,
        bot_service,
        billing as billing_api,
        panels,
        federation,
    )

    if is_sub():
        app.register_blueprint(subscription.bp, url_prefix="/api")
    elif is_bot_api():
        app.register_blueprint(bot_service.bp, url_prefix="/api")
        app.register_blueprint(billing_api.bp, url_prefix="/api")
    else:
        app.register_blueprint(auth.bp, url_prefix="/api")
        app.register_blueprint(inbound.bp, url_prefix="/api")
        app.register_blueprint(outbound.bp, url_prefix="/api")
        app.register_blueprint(routing.bp, url_prefix="/api")
        app.register_blueprint(system.bp, url_prefix="/api")
        app.register_blueprint(subscription.bp, url_prefix="/api")
        app.register_blueprint(statistics.bp, url_prefix="/api")
        if not is_worker():
            app.register_blueprint(bot_admin.bp, url_prefix="/api")
            app.register_blueprint(bot_service.bp, url_prefix="/api")
            app.register_blueprint(billing_api.bp, url_prefix="/api")
            app.register_blueprint(panels.bp, url_prefix="/api")
        app.register_blueprint(federation.bp, url_prefix="/api")

    if not is_sub() and not is_bot_api():
        bootstrap_defaults(app, sqlite_path)

    app.logger.info("backend ready (db=%s, scheduler started)", sqlite_path)
    return app
