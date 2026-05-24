import datetime as dt

from app.extensions import db
from app.models import BotEvent


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
    from app.jobs.notifications import cleanup_bot_events

    with app.app_context():
        cleanup_bot_events()
        assert BotEvent.query.get(eid) is None


def test_keeps_delivered_younger_than_7_days(app):
    eid = _insert_event(app, days_old=3, delivered_at=dt.datetime.utcnow())
    from app.jobs.notifications import cleanup_bot_events

    with app.app_context():
        cleanup_bot_events()
        assert BotEvent.query.get(eid) is not None


def test_deletes_undelivered_older_than_30_days(app):
    eid = _insert_event(app, days_old=31, delivered_at=None)
    from app.jobs.notifications import cleanup_bot_events

    with app.app_context():
        cleanup_bot_events()
        assert BotEvent.query.get(eid) is None


def test_keeps_undelivered_younger_than_30_days(app):
    eid = _insert_event(app, days_old=15, delivered_at=None)
    from app.jobs.notifications import cleanup_bot_events

    with app.app_context():
        cleanup_bot_events()
        assert BotEvent.query.get(eid) is not None
