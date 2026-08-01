from panel_core.app_base import (
    build_base_app,
    db_path,
    ensure_scheduler_job,
    migrate_schema,
    start_scheduler,
)
from panel_core.jobs.billing import auto_renew_free_users
from panel_core.jobs.notifications import cleanup_bot_events, replay_undelivered_bot_events
from panel_core.jobs.panels import poll_linked_panels, run_refresh_listener
from panel_core.panel_role import ROLE_CRON
from panel_core.services.version_check import fetch_latest
from panel_core.xray.gateway import NullXrayGateway, set_xray_gateway, xray_gateway_configured


def create_app():
    app = build_base_app(ROLE_CRON, public_surface=False)
    sqlite_path = db_path()

    if not xray_gateway_configured():
        set_xray_gateway(NullXrayGateway())

    migrate_schema(app, sqlite_path, drop_dead_tables=True)

    ensure_scheduler_job("poll_linked_panels", poll_linked_panels, 10)
    ensure_scheduler_job("replay_undelivered_bot_events", replay_undelivered_bot_events, 60)
    ensure_scheduler_job("auto_renew_free_users", auto_renew_free_users, 900)
    ensure_scheduler_job("cleanup_bot_events", cleanup_bot_events, 86400)
    ensure_scheduler_job("check_latest_version", fetch_latest, 21600)
    start_scheduler()

    try:
        import gevent

        gevent.spawn(_prime_version_cache, app)
        gevent.spawn(run_refresh_listener, app)
    except Exception:
        app.logger.warning("could not spawn background greenlets", exc_info=True)

    app.logger.info("cron service ready (db=%s, scheduler started, no HTTP surface)", sqlite_path)
    return app


def _prime_version_cache(app):
    with app.app_context():
        fetch_latest()
