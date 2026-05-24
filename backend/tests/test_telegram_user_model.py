"""Model-level checks for TelegramUser.language_chosen."""

from app.models import TelegramUser


def test_language_chosen_defaults_to_false(app, db):
    u = TelegramUser(telegram_id=111, language="ru")
    db.session.add(u)
    db.session.commit()
    fetched = TelegramUser.query.get(111)
    assert fetched.language_chosen is False


def test_language_chosen_persists_true(app, db):
    u = TelegramUser(telegram_id=222, language="en", language_chosen=True)
    db.session.add(u)
    db.session.commit()
    fetched = TelegramUser.query.get(222)
    assert fetched.language_chosen is True


def test_language_chosen_not_nullable(app, db):
    column = TelegramUser.__table__.columns["language_chosen"]
    assert column.nullable is False
    assert column.default.arg is False
