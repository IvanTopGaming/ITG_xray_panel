import datetime as dt

from panel_core.extensions import db
from panel_core.models import BotEvent


def _insert_event(app, *, days_old, delivered_at=None):
    with app.app_context():
        e = BotEvent(type="texts_changed", telegram_id=None, payload={})
        db.session.add(e)
        db.session.flush()
        e.created_at = dt.datetime.utcnow() - dt.timedelta(days=days_old)
        e.delivered_at = delivered_at
        db.session.commit()
        return e.id


def test_deletes_delivered_older_than_7_days(app):
    eid = _insert_event(app, days_old=8, delivered_at=dt.datetime.utcnow())
    from panel_core.jobs.notifications import cleanup_bot_events

    with app.app_context():
        cleanup_bot_events()
        assert db.session.get(BotEvent, eid) is None


def test_keeps_delivered_younger_than_7_days(app):
    eid = _insert_event(app, days_old=3, delivered_at=dt.datetime.utcnow())
    from panel_core.jobs.notifications import cleanup_bot_events

    with app.app_context():
        cleanup_bot_events()
        assert db.session.get(BotEvent, eid) is not None


def test_deletes_undelivered_older_than_30_days(app):
    eid = _insert_event(app, days_old=31, delivered_at=None)
    from panel_core.jobs.notifications import cleanup_bot_events

    with app.app_context():
        cleanup_bot_events()
        assert db.session.get(BotEvent, eid) is None


def test_keeps_undelivered_younger_than_30_days(app):
    eid = _insert_event(app, days_old=15, delivered_at=None)
    from panel_core.jobs.notifications import cleanup_bot_events

    with app.app_context():
        cleanup_bot_events()
        assert db.session.get(BotEvent, eid) is not None


def test_cleanup_removes_stale_notification_claims(app, db):
    import datetime as dt

    from panel_core.jobs.notifications import cleanup_bot_events
    from panel_core.models import NotificationClaim

    with app.app_context():
        old = NotificationClaim(telegram_id=1, tariff_id=1, scope="", kind="expiry_1d")
        old.created_at = dt.datetime.utcnow() - dt.timedelta(days=91)
        fresh = NotificationClaim(telegram_id=2, tariff_id=1, scope="", kind="expiry_1d")
        db.session.add_all([old, fresh])
        db.session.commit()

        cleanup_bot_events()

        remaining = [c.telegram_id for c in NotificationClaim.query.all()]
    assert remaining == [2]
