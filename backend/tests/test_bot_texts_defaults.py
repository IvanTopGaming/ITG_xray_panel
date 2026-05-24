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
