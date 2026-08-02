import os

import pytest

DSN = os.getenv("DATABASE_URL_TEST", "").strip()
pg_only = pytest.mark.skipif(not DSN, reason="DATABASE_URL_TEST not set")


def _build_source_sqlite(path):
    from sqlalchemy import create_engine, insert

    from panel_core.extensions import db
    import panel_core.models  # noqa: F401
    from panel_core.models import Admin, BotText, Client, SystemSetting

    eng = create_engine(f"sqlite:///{path}")
    db.metadata.create_all(eng)
    with eng.begin() as c:
        c.execute(
            insert(Client.__table__),
            [
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "email": "tg1_vless",
                    "inbound_tag": "vless",
                    "enable": True,
                    "up": 0,
                    "down": 0,
                    "expiry_time": 0,
                    "limit_bytes": 0,
                }
            ],
        )
        c.execute(
            insert(BotText.__table__),
            [
                {"key": "home.title", "lang": "ru", "text": "EDITED", "customized": True},
                {"key": "home.title", "lang": "en", "text": "Home", "customized": False},
            ],
        )
        c.execute(insert(SystemSetting.__table__), [{"key": "bot_texts_seeded_version", "value": "17"}])
        c.execute(insert(Admin.__table__), [{"username": "admin1", "password": "hash1"}])
    eng.dispose()


@pg_only
def test_import_copies_rows_and_coerces_types(tmp_path):
    from sqlalchemy import create_engine, text

    src_path = str(tmp_path / "panel.db")
    _build_source_sqlite(src_path)

    dst = create_engine(DSN)
    with dst.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))

    from sqlite_to_pg import import_sqlite_to_pg

    counts = import_sqlite_to_pg(src_path, DSN)

    assert counts["client"] == 1
    assert counts["bot_text"] == 2
    with dst.begin() as c:
        enable = c.execute(text("SELECT enable FROM client WHERE email='tg1_vless'")).scalar()
        cust = c.execute(text("SELECT customized FROM bot_text WHERE key='home.title' AND lang='ru'")).scalar()
        edited = c.execute(text("SELECT text FROM bot_text WHERE key='home.title' AND lang='ru'")).scalar()
        assert enable is True
        assert cust is True
        assert edited == "EDITED"


@pg_only
def test_import_resets_integer_pk_sequence(tmp_path):
    from sqlalchemy import create_engine, insert, text

    from panel_core.models import Admin

    src_path = str(tmp_path / "panel.db")
    _build_source_sqlite(src_path)

    dst = create_engine(DSN)
    with dst.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))

    from sqlite_to_pg import import_sqlite_to_pg

    import_sqlite_to_pg(src_path, DSN)

    with dst.begin() as c:
        max_id = c.execute(text("SELECT max(id) FROM admin")).scalar()
        c.execute(
            insert(Admin.__table__),
            [{"username": "admin2", "password": "hash2"}],
        )
        new_id = c.execute(text("SELECT id FROM admin WHERE username='admin2'")).scalar()
        assert new_id == max_id + 1


@pg_only
def test_import_refuses_non_empty_target_without_force(tmp_path):
    from sqlalchemy import create_engine, text

    src_path = str(tmp_path / "panel.db")
    _build_source_sqlite(src_path)

    dst = create_engine(DSN)
    with dst.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))

    from sqlite_to_pg import import_sqlite_to_pg

    import_sqlite_to_pg(src_path, DSN)
    with pytest.raises(RuntimeError):
        import_sqlite_to_pg(src_path, DSN)


@pg_only
def test_verify_counts_reports_parity(tmp_path):
    from sqlalchemy import create_engine, text

    src_path = str(tmp_path / "panel.db")
    _build_source_sqlite(src_path)

    dst = create_engine(DSN)
    with dst.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))

    from sqlite_to_pg import import_sqlite_to_pg, verify_counts

    import_sqlite_to_pg(src_path, DSN)
    mismatches = verify_counts(src_path, DSN)
    assert mismatches == []


@pg_only
def test_main_returns_zero_on_success(tmp_path):
    from sqlalchemy import create_engine, text

    src_path = str(tmp_path / "panel.db")
    _build_source_sqlite(src_path)

    dst = create_engine(DSN)
    with dst.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))

    from sqlite_to_pg import main

    rc = main(["--sqlite", src_path, "--pg", DSN])
    assert rc == 0
