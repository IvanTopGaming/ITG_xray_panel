"""Tests for the polished bot_text defaults + force-reseed behavior."""

import os
import sqlite3
import tempfile

import pytest
import yaml

from db_migration import CURRENT_BOT_TEXTS_VERSION, _maybe_force_reseed_bot_texts, _seed_bot_texts


@pytest.fixture
def seeded_db():
    """Fresh SQLite with a bot_text table and one pre-seeded row that has
    been 'admin-customized' (text differs from any YAML default)."""
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    conn = sqlite3.connect(path)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE bot_text (
            key VARCHAR(64) NOT NULL,
            lang VARCHAR(8) NOT NULL,
            text TEXT NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (key, lang)
        )
        """
    )
    cursor.execute(
        "INSERT INTO bot_text (key, lang, text) VALUES (?, ?, ?)",
        ("welcome.title", "ru", "OLD_CUSTOM_TEXT"),
    )
    conn.commit()
    yield conn, cursor
    conn.close()
    os.unlink(path)


def test_seed_without_force_skips_existing_row(seeded_db):
    conn, cursor = seeded_db
    inserted = _seed_bot_texts(cursor)
    conn.commit()
    cursor.execute("SELECT text FROM bot_text WHERE key='welcome.title' AND lang='ru'")
    assert cursor.fetchone()[0] == "OLD_CUSTOM_TEXT"
    # New rows from YAML still get inserted; old row preserved.
    assert inserted >= 1


def test_seed_with_force_overwrites_existing_row(seeded_db):
    conn, cursor = seeded_db
    inserted = _seed_bot_texts(cursor, force=True)
    conn.commit()
    cursor.execute("SELECT text FROM bot_text WHERE key='welcome.title' AND lang='ru'")
    text = cursor.fetchone()[0]
    assert text != "OLD_CUSTOM_TEXT"
    # The new welcome.title.ru carries the tech-vibe emoji
    assert text.startswith("🛡️")
    assert inserted >= 1


def _load_yaml():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "..", "app", "data", "bot_texts_defaults.yaml")
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# Keys that intentionally exist in ru only (first-touch picker)
_RU_ONLY_KEYS = {
    "lang_picker.title",
    "lang_picker.button.en",
    "lang_picker.button.ru",
}


def test_every_key_has_ru_and_en_except_picker():
    data = _load_yaml()
    for key, entry in data.items():
        if key in _RU_ONLY_KEYS:
            assert "ru" in entry, f"{key} missing ru variant"
            continue
        assert "ru" in entry, f"{key} missing ru variant"
        assert "en" in entry, f"{key} missing en variant"


def test_polished_emoji_spot_checks():
    data = _load_yaml()
    assert data["welcome.title"]["ru"].startswith("🛡️")
    assert data["welcome.title"]["en"].startswith("🛡️")
    assert data["welcome.body"]["ru"].startswith("⚡")
    assert data["menu.subscription"]["ru"].startswith("🔐")
    assert data["menu.tariffs"]["ru"].startswith("💎")
    assert data["menu.stats"]["ru"].startswith("📊")
    assert data["menu.help"]["ru"].startswith("📖")
    assert data["common.back"]["ru"].startswith("◀")
    assert data["common.cancel"]["ru"].startswith("❌")
    assert data["errors.access_denied"]["ru"].startswith("⛔")
    assert data["notification.expired"]["ru"].startswith("⛔")
    assert data["notification.expiry_1d"]["ru"].startswith("⚠️")


@pytest.fixture
def db_with_system_setting(seeded_db):
    """seeded_db already has bot_text + an admin-edited row. Add a
    system_setting table so the version helper can read/write it."""
    conn, cursor = seeded_db
    cursor.execute(
        """
        CREATE TABLE system_setting (
            key VARCHAR(100) PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.commit()
    return conn, cursor


def test_maybe_force_reseed_runs_when_stored_version_lower(db_with_system_setting):
    conn, cursor = db_with_system_setting
    # Stored version absent — defaults to 1, which is < CURRENT_BOT_TEXTS_VERSION
    ran = _maybe_force_reseed_bot_texts(cursor)
    conn.commit()
    assert ran is True

    # Existing admin-customised row is overwritten
    cursor.execute("SELECT text FROM bot_text WHERE key='welcome.title' AND lang='ru'")
    assert cursor.fetchone()[0].startswith("🛡️")

    # Stored version is bumped
    cursor.execute("SELECT value FROM system_setting WHERE key='bot_texts_seeded_version'")
    assert cursor.fetchone()[0] == str(CURRENT_BOT_TEXTS_VERSION)


def test_maybe_force_reseed_noop_when_already_current(db_with_system_setting):
    conn, cursor = db_with_system_setting
    cursor.execute(
        "INSERT INTO system_setting (key, value) VALUES (?, ?)",
        ("bot_texts_seeded_version", str(CURRENT_BOT_TEXTS_VERSION)),
    )
    conn.commit()

    ran = _maybe_force_reseed_bot_texts(cursor)
    conn.commit()
    assert ran is False

    # Existing admin-customised row preserved
    cursor.execute("SELECT text FROM bot_text WHERE key='welcome.title' AND lang='ru'")
    assert cursor.fetchone()[0] == "OLD_CUSTOM_TEXT"


def test_required_new_keys_exist():
    """Keys added by this polish pass — referenced by migrated handler/kb code."""
    data = _load_yaml()
    required = [
        "home.menu_header",
        "menu.subscription",
        "common.back",
        "common.back_to_main",
        "common.back_to_keys",
        "common.cancel",
        "sub.actions.show_qr",
        "sub.actions.show_stats",
        "keys.picker.title",
        "keys.list.entry",
        "keys.details.header",
        "keys.details.self_destruct",
        "keys.details.none",
        "security.timeout",
        "qr.select_title",
        "qr.server_label",
        "catalog.tariff_card.header",
        "catalog.tariff_card.item",
        "catalog.tariff_card.item.unlimited_amount",
        "catalog.tariff_card.item.gb_amount",
        "catalog.button.buy_inline",
        "catalog.button.active_marker",
        "catalog.empty",
        "catalog.pay_prompt",
        "catalog.payment_cancelled.message",
    ]
    missing = [k for k in required if k not in data]
    assert not missing, f"Missing required keys: {missing}"


def test_onboarding_keys_present_with_placeholder():
    data = _load_yaml()
    title = data.get("onboarding.title") or {}
    assert "ru" in title and "en" in title, "onboarding.title needs ru and en"
    assert "{user_name}" in title["ru"], "onboarding.title.ru must include {user_name}"
    assert "{user_name}" in title["en"], "onboarding.title.en must include {user_name}"

    skip = data.get("trial.button.skip") or {}
    assert "ru" in skip and "en" in skip, "trial.button.skip needs ru and en"


def test_stats_keys_present():
    data = _load_yaml()
    required = [
        "stats.header",
        "stats.user_line",
        "stats.grand_total",
        "stats.key.unavailable",
        "stats.key.status_active",
        "stats.key.status_disabled",
        "stats.key.used_label",
        "stats.key.left_label",
        "stats.key.unlimited",
        "stats.key.expiry_label",
        "stats.key.per_server",
        "stats.expiry.permanent",
        "stats.expiry.expired",
        "stats.expiry.hours_left",
        "stats.expiry.days_left",
    ]
    missing = [k for k in required if k not in data]
    assert not missing, f"Missing stats keys: {missing}"
    for key in required:
        entry = data[key]
        assert "ru" in entry and "en" in entry, f"{key} needs ru and en"
