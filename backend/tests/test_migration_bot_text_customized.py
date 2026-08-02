import os
import sqlite3
import tempfile

import pytest

from panel_core.db_migration import (
    _ensure_bot_text_customized_column,
    _load_bot_text_defaults,
    _seed_bot_texts,
)


@pytest.fixture
def bot_text_pre_v19():

    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    conn = sqlite3.connect(path)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE bot_text (
            key VARCHAR(120) NOT NULL,
            lang VARCHAR(8) NOT NULL,
            text TEXT NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (key, lang)
        )
        """
    )
    yield conn, cursor
    conn.close()
    os.unlink(path)


def test_reseed_preserves_admin_edits(bot_text_pre_v19):
    conn, cursor = bot_text_pre_v19
    defaults = _load_bot_text_defaults()
    assert defaults, "expected bot text defaults to load"
    items = list(defaults.items())
    (edited_key, edited_lang), edited_default = items[0]
    (pristine_key, pristine_lang), pristine_default = items[1]

    cursor.execute(
        "INSERT INTO bot_text (key, lang, text) VALUES (?, ?, ?)",
        (edited_key, edited_lang, edited_default + " [ADMIN EDIT]"),
    )
    cursor.execute(
        "INSERT INTO bot_text (key, lang, text) VALUES (?, ?, ?)",
        (pristine_key, pristine_lang, pristine_default),
    )
    conn.commit()

    marked = _ensure_bot_text_customized_column(cursor)
    assert marked == 1
    cursor.execute("SELECT customized FROM bot_text WHERE key=? AND lang=?", (edited_key, edited_lang))
    assert cursor.fetchone()[0] == 1
    cursor.execute("SELECT customized FROM bot_text WHERE key=? AND lang=?", (pristine_key, pristine_lang))
    assert cursor.fetchone()[0] == 0

    _seed_bot_texts(cursor, force=True)
    conn.commit()
    cursor.execute("SELECT text FROM bot_text WHERE key=? AND lang=?", (edited_key, edited_lang))
    assert cursor.fetchone()[0] == edited_default + " [ADMIN EDIT]"


def test_ensure_column_idempotent_no_remark(bot_text_pre_v19):
    conn, cursor = bot_text_pre_v19
    defaults = _load_bot_text_defaults()
    (key, lang), default = next(iter(defaults.items()))
    cursor.execute(
        "INSERT INTO bot_text (key, lang, text) VALUES (?, ?, ?)",
        (key, lang, default + " edited"),
    )
    conn.commit()

    assert _ensure_bot_text_customized_column(cursor) == 1

    assert _ensure_bot_text_customized_column(cursor) == 0
