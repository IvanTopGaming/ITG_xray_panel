"""§: three strings crossing a container boundary, with nothing on either side to check them.

The data tier's `offsite-backup` container is not part of the panel. It writes three rows into
`system_setting` after every successful pass and that is the entire return channel -- the master
never talks to it, never sees its logs and cannot ask it anything. Rename a key on one side and the
card goes quiet: no exception, no 404, no failing type check, just an off-site backup that reports
"never recorded" forever while working perfectly.

`test_the_script_and_the_panel_agree_on_every_key` is the only thing in the repo that would notice.

The staleness rule is measured in the container's *own* interval, which is why the script records
it: a fixed threshold would cry on a deployment that uploads daily and stay quiet for hours on one
that uploads every ten minutes. Three intervals is two missed cycles of slack.
"""

import pathlib
import time

from panel_core.models import SystemSetting
from panel_core.services.offsite import (
    FALLBACK_INTERVAL_SECONDS,
    INTERVAL_KEY,
    LAST_SUCCESS_KEY,
    REMOTE_KEY,
    STALE_AFTER_INTERVALS,
    read_status,
)

from tests.frontend_import_graph import PACKAGE_ROOTS

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "offsite_backup.sh"
SYSTEM_PAGE = PACKAGE_ROOTS["ui-core"] / "pages" / "System.tsx"
VERSION_LIB = PACKAGE_ROOTS["ui-core"] / "lib" / "version.ts"

PG_URI = "postgresql+psycopg2://panel:pw@data:5432/panel"


def _as_shared_postgres(app):
    """Flip the applicability verdict without needing a Postgres server.

    `read_status()` decides whether the question applies by reading the configured URI, and then
    runs one portable `SELECT` through whatever engine is bound. The conftest `app` fixture binds an
    in-memory SQLite; rewriting only the config string is what lets these tests exercise the
    Postgres branch of the verdict while the rows live somewhere cheap. The one thing it does not
    cover is the SQL dialect, which is why the statement uses nothing dialect-specific.
    """

    app.config["SQLALCHEMY_DATABASE_URI"] = PG_URI


def _mark(db, *, last_ms=None, interval=None, remote=None):
    pairs = {LAST_SUCCESS_KEY: last_ms, INTERVAL_KEY: interval, REMOTE_KEY: remote}
    for key, value in pairs.items():
        if value is not None:
            db.session.add(SystemSetting(key=key, value=str(value)))
    db.session.commit()


def test_the_script_and_the_panel_agree_on_every_key():
    body = SCRIPT.read_text()
    for key in (LAST_SUCCESS_KEY, INTERVAL_KEY, REMOTE_KEY):
        assert f"'{key}'" in body, (
            f"scripts/offsite_backup.sh never writes {key!r}, which panel_core.services.offsite "
            f"reads. The two halves live in different images on different machines and nothing "
            f"else in this repository connects them: a rename on either side leaves the panel "
            f"reporting 'never recorded' while the uploads carry on fine."
        )


def test_a_role_on_its_own_sqlite_is_not_asked_the_question(app, db):
    reading = read_status()
    assert reading == {"applicable": False}, (
        "a node keeps its own SQLite and never runs an offsite container, so an absent mark there "
        "means 'not this role', not 'never uploaded'. Reporting the latter would light a permanent "
        "red warning on every node in the fleet."
    )


def test_a_fresh_mark_reads_as_healthy(app, db):
    _as_shared_postgres(app)
    _mark(db, last_ms=int(time.time() * 1000), interval=1800, remote="gdrive:panel-backups")

    reading = read_status()

    assert reading["applicable"] and reading["available"]
    assert reading["stale"] is False
    assert reading["remote"] == "gdrive:panel-backups"
    assert reading["age_seconds"] < 60
    assert reading["stale_after_seconds"] == STALE_AFTER_INTERVALS * 1800


def test_a_mark_older_than_three_intervals_reads_as_stale(app, db):
    _as_shared_postgres(app)
    stopped = int((time.time() - 3 * 1800 - 60) * 1000)
    _mark(db, last_ms=stopped, interval=1800)

    reading = read_status()

    assert reading["stale"] is True, (
        "an upload that stopped over an hour and a half ago on a thirty-minute schedule still "
        "reads as healthy. That is the exact failure this feature exists to make visible."
    )


def test_a_mark_two_intervals_old_is_still_given_the_benefit_of_the_doubt(app, db):
    _as_shared_postgres(app)
    _mark(db, last_ms=int((time.time() - 2 * 1800) * 1000), interval=1800)

    assert read_status()["stale"] is False, (
        "one missed cycle -- a slow upload, a restart, a brief network wobble -- must not turn the "
        "card red, or the card becomes noise and stops being read."
    )


def test_an_unrecorded_interval_falls_back_rather_than_dividing_by_nothing(app, db):
    _as_shared_postgres(app)
    _mark(db, last_ms=int(time.time() * 1000))

    reading = read_status()

    assert reading["interval_seconds"] is None
    assert reading["stale_after_seconds"] == STALE_AFTER_INTERVALS * FALLBACK_INTERVAL_SECONDS


def test_no_mark_at_all_is_reported_as_such_rather_than_as_an_age(app, db):
    _as_shared_postgres(app)

    reading = read_status()

    assert reading["applicable"] and reading["available"]
    assert reading["last_success_at_ms"] is None
    assert reading["age_seconds"] is None
    assert reading["stale"] is False, (
        "'never recorded' and 'stopped working' are different facts and must not share a colour: "
        "the first is what a data tier that never wanted off-site copies looks like."
    )


def test_a_damaged_mark_does_not_take_the_card_down(app, db):
    _as_shared_postgres(app)
    db.session.add(SystemSetting(key=LAST_SUCCESS_KEY, value="not a number"))
    db.session.commit()

    reading = read_status()

    assert reading["last_success_at_ms"] is None
    assert reading["stale"] is False


def test_the_health_card_carries_the_reading(app, db):
    from panel_core.services.health import collect

    _as_shared_postgres(app)
    _mark(db, last_ms=int(time.time() * 1000), interval=1800)

    assert collect()["offsite_backup"]["stale"] is False


def test_one_unreadable_reading_does_not_take_the_others_with_it(app, db, monkeypatch):
    """services/health.py's contract, applied to the new reading.

    A card that cannot render because one of its four readings failed tells an admin less than a
    card that says which one failed.
    """

    from panel_core.services import health

    monkeypatch.setattr(health, "read_offsite_status", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    collected = health.collect()

    assert collected["offsite_backup"] == {"applicable": True, "available": False}
    assert collected["data_tier"]["database"] == "ok"


def test_the_line_is_gated_to_the_master_in_the_bundle():
    """ui-core ships into both frontend images, so the role gate is what keeps this off a node.

    The backend half already answers `applicable: false` on a node's SQLite; this is the second
    level, and it is the one that decides whether the line is drawn at all. `test_a_role_on_its_own_
    sqlite_is_not_asked_the_question` above holds the other end.
    """

    body = SYSTEM_PAGE.read_text(encoding="utf-8")

    assert "health.offsite_backup" in body, "the health card never reads the offsite reading"
    assert "if (!isWorker && offsite.applicable) {" in body, (
        "the off-site line is not gated on the role. A node runs no offsite container and its "
        "SQLite never receives a mark, so the line would read 'never recorded' on every node in "
        "the fleet -- a permanent red warning about a machine that has nothing to do with it."
    )


def test_the_three_tones_are_three_different_facts():
    body = SYSTEM_PAGE.read_text(encoding="utf-8")

    assert "'never recorded'" in body, (
        "no mark at all must say so rather than showing an age; it is what a data tier that never "
        "wanted off-site copies looks like, and colouring it like a failure makes the card noise"
    )
    assert "return reading.stale ? 'bad' : 'ok';" in body, "a stale mark must be red"


def test_the_frontend_type_matches_what_the_reading_returns():
    body = VERSION_LIB.read_text(encoding="utf-8")

    for field in ("applicable", "last_success_at_ms", "stale_after_seconds", "stale", "remote"):
        assert field in body, (
            f"SystemHealth does not declare {field!r}. The type is the only check on this "
            f"boundary -- the endpoint returns a plain dict and TypeScript never sees the Python."
        )
