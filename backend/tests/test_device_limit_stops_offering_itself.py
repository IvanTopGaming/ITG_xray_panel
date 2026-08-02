"""Wave 4d: two fields that have constrained nothing since wave 3b stop asking to be edited.

`Client.device_limit` and `Inbound.device_limit` are the monolith's per-key and per-inbound device
caps. Wave 3b replaced them with **one global budget per Telegram account**: `user_device` is unique
on `(telegram_id, hwid)`, and `services/device_tracking.user_device_gate` reads exactly two settings
— `device_limit_enabled` and `device_limit_per_user`. Nothing joins through `Client` any more, and
nothing anywhere reads either column to refuse a device.

The columns survived, and so did everything around them: the API accepted them, the snapshot carried
them, both forms edited them, and the Dashboard rendered `2 / 3` where the numerator came from the
real global ledger and the denominator from the dead field. So an admin could set a cap of 3, be told
it saved, and have nothing happen — the same class as `PUBLISH` with no subscriber marking an event
delivered, and the one this project keeps finding.

**The columns stay, deliberately.** Dropping a column from an existing table is the one thing the
Postgres migration path cannot do (INFRA §40): `migrate_postgres_db` is `create_all` plus dropping FK
constraints plus `DROP TABLE`, with no `ALTER … DROP COLUMN` anywhere. Removing them is a schema
change that has to be its own wave. Until then the rows keep whatever number they were last given and
nobody can see it — which is the correct end state for a value nothing honours.
"""

import pathlib
import re

from panel_core.models import Client, Inbound

from tests.frontend_import_graph import PACKAGE_ROOTS

BACKEND = pathlib.Path(__file__).resolve().parents[1] / "packages"

GLOBAL_SETTINGS = ("device_limit_enabled", "device_limit_per_user")
SNAPSHOT_STRIP = 'ib_data.pop("device_limit", None)'


def _mentions(path: pathlib.Path) -> list[str]:
    hits = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if "device_limit" not in line:
            continue
        if any(setting in line for setting in GLOBAL_SETTINGS):
            continue
        if SNAPSHOT_STRIP in line:
            continue
        hits.append(line.strip())
    return hits


class TestTheColumnsStay:
    def test_both_are_still_declared(self):
        """A "cleanup" that drops them is a schema change, not a form change — see the docstring."""

        assert Client.__table__.columns.get("device_limit") is not None
        assert Inbound.__table__.columns.get("device_limit") is not None

    def test_the_migration_still_adds_them_to_an_old_database(self):
        body = (BACKEND / "panel-core/src/panel_core/db_migration.py").read_text(encoding="utf-8")

        assert '("inbound", "device_limit"' in body
        assert '("client", "device_limit"' in body


class TestTheApiNeitherAcceptsNorReturnsIt:
    def test_no_handler_reads_it_off_a_request(self):
        offenders = {
            str(path.relative_to(BACKEND)): _mentions(path)
            for path in (BACKEND / "panel-adminapi").rglob("*.py")
            if _mentions(path) and path.name != "db_migration.py"
        }

        assert offenders == {}, (
            f"device_limit is still part of the admin API surface: {offenders}. Accepting a value "
            f"nothing enforces is the defect; the column is allowed to exist, the field is not."
        )

    def test_the_client_serialiser_does_not_hand_it_out(self):
        body = (BACKEND / "panel-core/src/panel_core/models.py").read_text(encoding="utf-8")
        to_dict = body.split("def to_dict(self):", 1)[1].split("\n\nclass ", 1)[0]

        assert "device_limit" not in to_dict, (
            "Client.to_dict() still returns device_limit; it reaches the Dashboard, the bot's state "
            "endpoint and the master's user drawer at once"
        )

    def test_the_federation_snapshot_does_not_carry_it(self):
        body = (BACKEND / "panel-worker/src/panel_core/api/federation.py").read_text(encoding="utf-8")

        assert "device_limit" not in body

    def test_a_node_on_an_older_image_cannot_reintroduce_it(self):
        """The master merges each node's snapshot into `GET /api/inbounds` verbatim.

        An un-upgraded node still sends the field. Stripping it in the overlay is what makes the
        master's own response shape independent of how far the fleet has been rolled out.
        """

        body = (BACKEND / "panel-adminapi/src/panel_core/api/inbound.py").read_text(encoding="utf-8")

        assert SNAPSHOT_STRIP in body


class TestNoFormOffersIt:
    def test_it_is_gone_from_every_frontend_package(self):
        offenders = {}
        for root in PACKAGE_ROOTS.values():
            for path in root.rglob("*.ts*"):
                if path.suffix not in (".ts", ".tsx"):
                    continue
                hits = _mentions(path)
                if hits:
                    offenders[path.name] = hits

        assert offenders == {}, (
            f"a device_limit field or read survives in the UI: {offenders}. Editing it succeeds and "
            f"changes nothing; rendering it puts a cap on screen that no request is measured against."
        )

    def test_the_global_setting_is_still_editable(self):
        """The negative above must not have been satisfied by deleting the limit that does work."""

        settings_tab = PACKAGE_ROOTS["admin"] / "components" / "bot" / "SettingsTab.tsx"
        body = settings_tab.read_text(encoding="utf-8")

        for setting in GLOBAL_SETTINGS:
            assert setting in body, f"{setting} is the only device limit there is; it must stay editable"

    def test_the_device_counter_lost_its_denominator_only(self):
        """The numerator is the real global count and must survive."""

        body = (PACKAGE_ROOTS["ui-core"] / "pages" / "Dashboard.tsx").read_text(encoding="utf-8")

        assert "client.device_count" in body
        assert not re.search(r"/\s*\$\{effectiveDeviceLimit", body)
        assert "effectiveDeviceLimit" not in body
