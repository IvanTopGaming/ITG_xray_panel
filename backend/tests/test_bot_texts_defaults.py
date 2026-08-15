import pytest
import yaml

from tests.import_graph import source_path


def _load_defaults():
    path = source_path("data/bot_texts_defaults.yaml")
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def test_lang_picker_title_seeded_in_ru():
    data = _load_defaults()
    entry = data.get("lang_picker.title", {})
    assert "Привет" in entry.get("ru", "")


def test_lang_picker_button_keys_seeded():
    data = _load_defaults()
    en_btn = data.get("lang_picker.button.en", {}).get("ru", "")
    ru_btn = data.get("lang_picker.button.ru", {}).get("ru", "")
    assert "English" in en_btn
    assert "Русский" in ru_btn


def test_new_subscription_page_keys_present_both_langs():
    data = _load_defaults()
    required = [
        "sub.page.title",
        "sub.page.link_header",
        "sub.page.copy_hint",
        "sub.page.url_helper",
        "sub.page.no_url",
        "sub.actions.open_page",
        "sub.actions.show_keys",
    ]
    for key in required:
        assert key in data, f"missing key {key}"
        assert data[key].get("ru"), f"{key} missing ru"
        assert data[key].get("en"), f"{key} missing en"


def test_bot_texts_version_bumped_to_20():
    from panel_core.db_migration import CURRENT_BOT_TEXTS_VERSION

    assert CURRENT_BOT_TEXTS_VERSION == 20, (
        "20 retires notification.access_paused and notification.access_renewed, which belonged to "
        "the removed automatic grant renewal. A key "
        "dropped from the YAML is purged only while stored < CURRENT, so removing one without "
        f"bumping this leaves an orphan row on every live database; got {CURRENT_BOT_TEXTS_VERSION}"
    )


def test_every_key_dropped_from_the_yaml_is_listed_as_retired():
    """A key removed from the YAML but not from `_REMOVED_BOT_TEXT_KEYS` lingers forever.

    The seeder only inserts and updates; nothing deletes. So a key dropped from the defaults stays
    on every live database as an orphan row the admin can still see and edit in Bot → Texts, while
    no code reads it. The force-reseed's DELETE list is the only thing that removes one, and it is
    hand-maintained -- which is exactly the kind of list that goes stale silently.
    """

    import pathlib
    import subprocess

    from panel_core.db_migration import _REMOVED_BOT_TEXT_KEYS

    repo = pathlib.Path(__file__).resolve().parents[2]
    relative = "backend/packages/panel-core/src/panel_core/data/bot_texts_defaults.yaml"

    current = set(_load_defaults())
    assert current, "the defaults file parsed to no keys at all — this guard would pass vacuously"

    previous_raw = subprocess.run(
        ["git", "show", f"HEAD:{relative}"],
        capture_output=True,
        text=True,
        cwd=repo,
    )
    if previous_raw.returncode != 0:
        pytest.skip("no committed version of the defaults to diff against")

    previous = set(yaml.safe_load(previous_raw.stdout) or {})
    assert previous, "the committed defaults parsed to no keys — this guard would pass vacuously"

    unlisted = sorted(key for key in previous - current if key not in _REMOVED_BOT_TEXT_KEYS)
    assert unlisted == [], (
        f"these keys left bot_texts_defaults.yaml without being added to _REMOVED_BOT_TEXT_KEYS: "
        f"{unlisted}. Nothing else deletes a bot text, so each one survives on every live database."
    )
