from sqlalchemy import Boolean, Column, String
from sqlalchemy.dialects import postgresql

import panel_core.models  # noqa: F401
from panel_core.models import LinkedPanel, PanelStateMirror
from panel_core.pg_migrate import _column_ddl

PG = postgresql.dialect()


def _column(model, name):
    return model.__table__.columns[name]


def test_boolean_not_null_columns_default_to_the_pg_keyword_not_an_integer_literal():
    for model, name, expected in (
        (LinkedPanel, "transfer_token_used", "false"),
        (LinkedPanel, "transfer_carry_admin", "true"),
        (PanelStateMirror, "shrink_flagged", "false"),
    ):
        ddl, forced_nullable = _column_ddl(_column(model, name), dialect=PG)

        assert not forced_nullable, f"{model.__tablename__}.{name}: lost its NOT NULL: {ddl!r}"
        assert ddl.endswith(f"DEFAULT {expected} NOT NULL"), (
            f"{model.__tablename__}.{name}: Postgres has no assignment cast from integer to boolean — "
            f"a bare 0/1 default breaks ALTER TABLE ADD COLUMN on an existing table: {ddl!r}"
        )


def test_string_not_null_columns_quote_their_default_instead_of_leaving_it_dangling():
    for model, name, expected in (
        (LinkedPanel, "transfer_state", "''"),
        (PanelStateMirror, "kind", "'current'"),
        (PanelStateMirror, "hot_state", "''"),
        (PanelStateMirror, "cold_state", "''"),
    ):
        ddl, forced_nullable = _column_ddl(_column(model, name), dialect=PG)

        assert not forced_nullable, f"{model.__tablename__}.{name}: lost its NOT NULL: {ddl!r}"
        assert ddl.endswith(f"DEFAULT {expected} NOT NULL"), (
            f"{model.__tablename__}.{name}: an unquoted empty-string default renders as a dangling "
            f"'DEFAULT' with nothing after it — a syntax error on any dialect: {ddl!r}"
        )


def test_numeric_not_null_default_is_a_castable_literal():
    ddl, forced_nullable = _column_ddl(_column(PanelStateMirror, "taken_at"), dialect=PG)

    assert not forced_nullable
    assert ddl == "\"taken_at\" BIGINT DEFAULT '0' NOT NULL"


def test_column_ddl_quotes_scalar_defaults_including_empty_strings():
    booby_trapped_bool = Column("flag", Boolean, nullable=False, server_default="0")
    ddl, forced_nullable = _column_ddl(booby_trapped_bool, dialect=PG)
    assert not forced_nullable
    assert ddl == "\"flag\" BOOLEAN DEFAULT '0' NOT NULL"

    booby_trapped_empty_string = Column("label", String(10), nullable=False, server_default="")
    ddl, forced_nullable = _column_ddl(booby_trapped_empty_string, dialect=PG)
    assert not forced_nullable
    assert ddl == "\"label\" VARCHAR(10) DEFAULT '' NOT NULL"
