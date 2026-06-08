"""Ensure picker keys are seeded in bot_texts_defaults.yaml."""

import os

import yaml


def _load_defaults():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "..", "app", "data", "bot_texts_defaults.yaml")
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


def test_bot_texts_version_bumped_to_17():
    from db_migration import CURRENT_BOT_TEXTS_VERSION

    assert CURRENT_BOT_TEXTS_VERSION == 17
