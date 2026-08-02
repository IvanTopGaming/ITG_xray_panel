"""§23 / §8.16: a payment left in 'processing' by a crash must return to the queue in minutes.

`apply_payment` moves a payment `pending → processing` with a single atomic UPDATE, and that claim
is the only thing that stops two hosts granting the same tariff twice. If the process dies between
the claim and the end of provisioning, the row is left in a status nothing was looking for: the
poll took `status == 'pending'`, the webhook re-entered `apply_payment` and returned at
`claim.rowcount == 0`, and the user had paid for something that would never be delivered.

It was not quite "never" by the time this was written -- `cleanup_old_payments` learned to release
such a row -- but that job runs every 24 hours and only considers payments older than 24 hours, so
the floor was a day and a half. This puts it at minutes.

**The load-bearing part is what was NOT done.** Widening the claim to
`WHERE status IN ('pending','processing')` would close the same gap in one line and reopen the
double-grant it exists to prevent. Recovery is a separate branch that puts the row back to
'pending' and lets the ordinary path have it; the claim is asserted unchanged below, because that
one-line "simplification" is the obvious thing for a future reader to reach for.

**The age is measured in the process, not in the row.** `Payment` has no `updated_at`, and adding
a column to an existing table is the one thing the Postgres migration path cannot do (§40).
`created_at` cannot stand in for it: the poll reaches back 24 hours, so a payment is routinely
claimed long after it was created, and a `created_at`-based rule would release live claims. So the
job remembers which ids it has already seen in 'processing'. Losing that map -- the process
restarting -- is the same event that strands a claim in the first place, and it costs one extra
cycle, never a wrong release.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import patch

import pytest
import sqlalchemy as sa

from panel_core.extensions import db
from panel_core.models import Payment, SystemSetting, Tariff, TariffItem

from tests.import_graph import source_path


@pytest.fixture
def configured(app):
    with app.app_context():
        db.session.add_all(
            [
                SystemSetting(key="yookassa_shop_id", value="test-shop"),
                SystemSetting(key="yookassa_secret_key", value="test_secret"),
            ]
        )
        db.session.commit()


@pytest.fixture
def tariff(app, configured):
    with app.app_context():
        t = Tariff(name="Standard", price_rub=150, period_days=30, visibility="public", enabled=True, is_trial=False)
        t.items = [TariffItem(inbound_tag="vless-de", traffic_gb=0)]
        db.session.add(t)
        db.session.commit()
        yield t.id


@pytest.fixture(autouse=True)
def _forget_seen():
    from panel_core.jobs import payments

    payments._seen_processing.clear()
    yield
    payments._seen_processing.clear()


def _insert(app, tariff_id, status, *, age_seconds=600, yk_id="yk-stranded"):
    with app.app_context():
        p = Payment(
            yookassa_id=yk_id,
            telegram_id=42,
            tariff_id=tariff_id,
            tariff_snapshot={"name": "x", "price_rub": 150, "period_days": 30, "items": []},
            amount_rub=150,
            status=status,
            confirmation_url="https://yookassa.test/pay/x",
            metadata_json={"lang": "ru"},
        )
        db.session.add(p)
        db.session.flush()
        p.created_at = dt.datetime.utcnow() - dt.timedelta(seconds=age_seconds)
        db.session.commit()
        return p.id


def _status(app, pid):
    with app.app_context():
        return db.session.get(Payment, pid).status


def test_a_claim_held_for_a_moment_is_left_alone(app, tariff):
    """The first sighting only starts the clock — an in-flight apply_payment must not be disturbed."""

    from panel_core.jobs.payments import release_stranded_claims

    pid = _insert(app, tariff, "processing")
    with app.app_context():
        release_stranded_claims()

    assert _status(app, pid) == "processing", (
        "a payment claimed seconds ago was released. Provisioning a multi-node tariff onto an "
        "unreachable node legitimately takes tens of seconds; releasing it there would duplicate the "
        "user's 'payment received' message every cycle."
    )


def test_a_claim_nobody_finished_goes_back_to_pending(app, tariff, monkeypatch):
    from panel_core.jobs import payments

    pid = _insert(app, tariff, "processing")
    with app.app_context():
        payments.release_stranded_claims()
        assert _status(app, pid) == "processing"

        clock = [0.0]
        monkeypatch.setattr(payments.time, "monotonic", lambda: clock[0])
        payments._seen_processing[pid] = 0.0
        clock[0] = payments._STRANDED_AFTER_S + 1
        payments.release_stranded_claims()

    assert _status(app, pid) == "pending", (
        "the row stayed in 'processing'. Nothing looks for that status on the paid path, so the user's "
        "money is taken and the grant never arrives until the 24-hour cleanup notices."
    )


def test_a_released_payment_is_then_applied_by_the_ordinary_poll(app, tariff, monkeypatch):
    """End to end: the point is not the status change, it is that the grant finally happens."""

    from panel_core.jobs import payments

    pid = _insert(app, tariff, "processing")
    with app.app_context():
        payments._seen_processing[pid] = 0.0
        monkeypatch.setattr(payments.time, "monotonic", lambda: payments._STRANDED_AFTER_S + 1)
        with (
            patch("panel_core.jobs.payments.yookassa.Payment.find_one") as find_one,
            patch("panel_core.services.billing.provisioning.apply_tariff_for_user") as provision,
            patch("panel_core.services.billing.bot_events.publish"),
        ):
            find_one.return_value = type("YK", (), {"status": "succeeded"})()
            provision.return_value = {"clients": [], "expires_at_ms": 9999999999000, "source": "yookassa"}
            payments.poll_pending_payments()

    assert _status(app, pid) == "succeeded"


def test_a_payment_that_left_processing_is_forgotten(app, tariff, monkeypatch):
    """Otherwise the map grows for the life of the process and holds ids that no longer exist."""

    from panel_core.jobs import payments

    pid = _insert(app, tariff, "processing")
    with app.app_context():
        payments.release_stranded_claims()
        assert pid in payments._seen_processing

        db.session.get(Payment, pid).status = "succeeded"
        db.session.commit()
        payments.release_stranded_claims()

    assert pid not in payments._seen_processing


def test_the_atomic_claim_was_not_widened(app, tariff):
    """The one-line 'simplification' this whole branch exists to avoid.

    Asserted twice: in the source, because the intent is a single word in a WHERE clause, and in
    behaviour, because a textual guard alone would pass on an equivalent rewrite.
    """

    source = source_path("services/billing.py").read_text()
    assert 'Payment.status == "pending"' in source, "the claim in apply_payment no longer matches only 'pending'"
    assert "Payment.status.in_" not in source, (
        "the claim was widened to accept 'processing'. That is what stops a second host from granting "
        "the same tariff again while the first is still provisioning — recovery belongs in "
        "release_stranded_claims, not in the claim."
    )

    from panel_core.services import billing

    pid = _insert(app, tariff, "processing")
    with app.app_context():
        payment = db.session.get(Payment, pid)
        with patch("panel_core.services.billing.provisioning.apply_tariff_for_user") as provision:
            billing.apply_payment(payment)
        assert not provision.called, "apply_payment granted a tariff for a payment it did not claim"

    assert _status(app, pid) == "processing"


def test_the_poll_actually_runs_the_release(app, tariff):
    """A branch nothing calls is the same as no branch."""

    from panel_core.jobs import payments

    called = []
    with app.app_context():
        with (
            patch.object(payments, "release_stranded_claims", lambda: called.append(1)),
            patch("panel_core.jobs.payments.yookassa.Payment.find_one", side_effect=AssertionError("no payments")),
        ):
            payments.poll_pending_payments()

    assert called == [1], "poll_pending_payments no longer releases stranded claims"


def test_the_twenty_four_hour_backstop_is_still_there(app, tariff):
    """Kept deliberately: this branch now fires first, but it is the only one that runs if the poll does not."""

    source = source_path("jobs/payments.py").read_text()
    assert source.count("stranded in 'processing'") == 1
    assert sa is not None
