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


def test_postgres_utc_clock_preserves_other_connection_options(monkeypatch):
    monkeypatch.setenv("PGOPTIONS", "-c statement_timeout=5000 -c timezone=Europe/Moscow")
    options = engine_options("postgresql://u:p@host/db")["connect_args"]["options"]
    assert options.endswith("-c timezone=UTC")
    assert "statement_timeout=5000" in options
    options = engine_options("postgresql://u:p@host/db?options=-c%20statement_timeout%3D2000")["connect_args"][
        "options"
    ]
    assert options == "-c statement_timeout=2000 -c timezone=UTC"


def test_postgres_timestamp_defaults_match_utc_application_clock(monkeypatch):
    import os
    from sqlalchemy import create_engine, text

    uri = os.getenv("DATABASE_URL_TEST", "")
    if not is_postgres(uri):
        pytest.skip("DATABASE_URL_TEST must point to PostgreSQL")
    monkeypatch.setenv("PGOPTIONS", "-c timezone=Europe/Moscow")
    engine = create_engine(uri, **engine_options(uri))
    try:
        with engine.connect() as connection, connection.begin():
            connection.execute(
                text("CREATE TEMP TABLE clock_probe (created_at timestamp DEFAULT current_timestamp) ON COMMIT DROP")
            )
            connection.execute(text("INSERT INTO clock_probe DEFAULT VALUES"))
            delta = connection.scalar(
                text(
                    "SELECT EXTRACT(EPOCH FROM (created_at - (current_timestamp AT TIME ZONE 'UTC'))) FROM clock_probe"
                )
            )
            assert delta == 0
    finally:
        engine.dispose()


def test_engine_options_for_postgres():
    opts = engine_options("postgresql://u:p@h/db")
    assert opts["pool_pre_ping"] is True
    assert opts["pool_recycle"] == 1800
    assert opts["pool_size"] == 5
    assert opts["max_overflow"] == 10


def test_postgres_connections_fail_fast_when_the_tier_is_unreachable():
    """§114: without this the request hangs instead of erroring.

    libpq waits indefinitely by default, so a data tier that disappears at the network level — the
    VM off, a partition, a firewall, which is exactly the failure a separate VM exists to survive —
    turned every request into a wait with no answer. Measured on a live stand: a subscription
    request with Postgres unreachable produced nothing at all in 25 seconds, no status and no error,
    which a client app shows as a spinner rather than a failure. `pool_pre_ping` makes it worse
    rather than better, because it round-trips on every checkout.
    """

    opts = engine_options("postgresql+psycopg2://u:p@data-vm/db?sslmode=verify-full")
    assert opts["connect_args"]["connect_timeout"] > 0, (
        "no connect timeout reaches libpq, so an unreachable data tier hangs every request until the "
        "OS gives up on the TCP connection"
    )


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
