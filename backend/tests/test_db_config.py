import pytest
from panel_core.db_config import database_uri, engine_options, is_postgres, validate_database_uri


def test_database_uri_defaults_to_sqlite(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert database_uri("/data/panel.db") == "sqlite:////data/panel.db"


def test_database_uri_prefers_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/panel?sslmode=verify-full")
    assert database_uri("/data/panel.db") == "postgresql://u:p@h:5432/panel?sslmode=verify-full"


def test_database_uri_ignores_blank_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "   ")
    assert database_uri("/data/panel.db").startswith("sqlite:///")


@pytest.mark.parametrize(
    "uri,expected",
    [
        ("postgresql://u:p@h/db", True),
        ("postgresql+psycopg2://u:p@h/db", True),
        ("sqlite:////data/panel.db", False),
    ],
)
def test_is_postgres(uri, expected):
    assert is_postgres(uri) is expected


def test_engine_options_empty_for_sqlite():
    assert engine_options("sqlite:////data/panel.db") == {}


def test_engine_options_for_postgres():
    opts = engine_options("postgresql://u:p@h/db")
    assert opts["pool_pre_ping"] is True
    assert opts["pool_recycle"] == 1800
    assert opts["pool_size"] == 5
    assert opts["max_overflow"] == 10


def test_validate_rejects_insecure_pg_in_production():
    with pytest.raises(RuntimeError):
        validate_database_uri("postgresql://u:p@h/db", is_local=False)


def test_validate_rejects_wrong_sslmode_in_production():
    with pytest.raises(RuntimeError):
        validate_database_uri("postgresql://u:p@h/db?sslmode=require", is_local=False)


def test_validate_allows_verify_full_in_production():
    validate_database_uri("postgresql://u:p@h/db?sslmode=verify-full", is_local=False)


def test_validate_allows_insecure_pg_locally():
    validate_database_uri("postgresql://u:p@h/db", is_local=True)


def test_validate_ignores_sqlite():
    validate_database_uri("sqlite:////data/panel.db", is_local=False)


def test_validate_rejects_alt_driver_scheme_without_verify_full():
    with pytest.raises(RuntimeError):
        validate_database_uri("postgresql+pg8000://u:p@h/db", is_local=False)
