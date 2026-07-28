"""§8.9 step 5 + §27 point 3: the master stops owning outbounds it has nothing to route with.

`bootstrap_defaults` seeded `direct` and `block` on every role that called it, and re-enabled them
if an admin had switched them off. On a node that is right — they are the two outbounds every Xray
config needs. On a master it was the last thing making "the master has no outbounds of its own"
false: `GET /outbounds` there answered `200 [direct, block]`, which is the seed talking, not the
fleet.

The seed is now driven by the role, not by shared code — both roles call the same function, so
switching it off in `bootstrap_defaults` itself would have silently disarmed every node. The
master passes `system_outbounds=False`, which does not merely skip the seed but **removes what an
earlier release already wrote**: those two rows are sitting in the Postgres of every live master,
and skipping the seed alone would leave the claim untrue forever (customer decision).

The removal is deliberately narrow — only the two system tags, and only on the role that asked for
it. Anything else an old monolith-turned-master happens to hold is left alone; nothing reads it,
and a boot-time `DELETE` with a wider reach is not something to hand a live database.

The node app is built **without `DATABASE_URL`** (§48): with one set, `migrate_sqlite_db` and the
ORM address different files, and "the node still has its outbounds" would be read out of an empty
database and pass for the wrong reason.
"""

import pytest

from panel_core.extensions import db
from panel_core.models import Outbound

from tests.schema import ensure_schema


def _reset_scheduler():
    from panel_core.extensions import scheduler

    if scheduler.running:
        scheduler.shutdown(wait=False)
    for job in list(scheduler.get_jobs()):
        scheduler.remove_job(job.id)


@pytest.fixture(autouse=True)
def _scheduler_teardown():
    yield
    _reset_scheduler()


def _build(role, monkeypatch, tmp_path, database_url=None):
    from panel_core.xray import gateway as gw

    monkeypatch.setenv("PANEL_ROLE", role)
    if database_url is None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.chdir(tmp_path)
    _reset_scheduler()
    gw.set_xray_gateway(None)

    import importlib

    return importlib.import_module(f"panel_core.roles.{role}").create_app()


class TestTheMasterDropsTheSeededOutbounds:
    def test_rows_an_earlier_release_seeded_are_removed_on_boot(self, monkeypatch, tmp_path):
        """The half that skipping the seed does not cover: live masters already have these rows."""

        uri = ensure_schema(f"sqlite:///{tmp_path}/master.db")

        first = _build("master", monkeypatch, tmp_path, database_url=uri)
        with first.app_context():
            db.session.add(Outbound(tag="direct", protocol="freedom", enable=True))
            db.session.add(Outbound(tag="block", protocol="blackhole", enable=True))
            db.session.commit()
            assert Outbound.query.count() == 2

        _reset_scheduler()
        second = _build("master", monkeypatch, tmp_path, database_url=uri)
        with second.app_context():
            assert Outbound.query.count() == 0

    def test_the_removal_is_narrow_enough_to_run_against_a_live_database(self, monkeypatch, tmp_path):
        uri = ensure_schema(f"sqlite:///{tmp_path}/master.db")

        first = _build("master", monkeypatch, tmp_path, database_url=uri)
        with first.app_context():
            db.session.add(Outbound(tag="direct", protocol="freedom", enable=True))
            db.session.add(Outbound(tag="leftover-from-the-monolith", protocol="freedom", enable=True))
            db.session.commit()

        _reset_scheduler()
        second = _build("master", monkeypatch, tmp_path, database_url=uri)
        with second.app_context():
            assert {o.tag for o in Outbound.query.all()} == {"leftover-from-the-monolith"}

    def test_a_second_boot_on_an_already_clean_database_is_a_no_op(self, monkeypatch, tmp_path):
        uri = ensure_schema(f"sqlite:///{tmp_path}/master.db")

        _build("master", monkeypatch, tmp_path, database_url=uri)
        _reset_scheduler()
        again = _build("master", monkeypatch, tmp_path, database_url=uri)

        with again.app_context():
            assert Outbound.query.count() == 0

    def test_the_endpoint_reports_the_truth_rather_than_the_seed(self, monkeypatch, tmp_path):
        app = _build("master", monkeypatch, tmp_path, database_url=ensure_schema(f"sqlite:///{tmp_path}/master.db"))

        import datetime

        import jwt as jwt_lib

        from panel_core.models import Admin
        from panel_core.utils import SECRET_KEY

        with app.app_context():
            admin = Admin.query.first()
            token = jwt_lib.encode(
                {
                    "user": admin.username,
                    "admin_id": admin.id,
                    "role": "admin",
                    "pwdv": int(admin.password_changed_at or 0),
                    "exp": datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2),
                },
                SECRET_KEY,
                algorithm="HS256",
            )

        resp = app.test_client().get("/api/outbounds", headers={"Authorization": f"Bearer {token}"})

        assert resp.status_code == 200
        assert resp.get_json() == []


class TestTheNodeKeepsThem:
    """The same shared function still seeds where the seed belongs — the mutation that matters is
    switching it off in `bootstrap_defaults` rather than at the master's call site."""

    def test_a_fresh_node_gets_direct_and_block(self, monkeypatch, tmp_path):
        app = _build("worker", monkeypatch, tmp_path)

        with app.app_context():
            assert {o.tag for o in Outbound.query.all()} == {"direct", "block"}

    def test_a_node_re_enables_them_if_an_admin_switched_them_off(self, monkeypatch, tmp_path):
        app = _build("worker", monkeypatch, tmp_path)
        with app.app_context():
            for ob in Outbound.query.all():
                ob.enable = False
            db.session.commit()

        _reset_scheduler()
        again = _build("worker", monkeypatch, tmp_path)
        with again.app_context():
            assert all(o.enable for o in Outbound.query.all())
