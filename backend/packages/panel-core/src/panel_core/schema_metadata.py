from sqlalchemy import UniqueConstraint
from sqlalchemy.schema import CreateIndex


def model_metadata():
    from panel_core import models

    return models.db.metadata


def column_ddl(column, dialect):
    name = dialect.identifier_preparer.quote_identifier(column.name)
    pieces = [f"{name} {column.type.compile(dialect=dialect)}"]
    default = dialect.ddl_compiler(dialect, None).get_column_default_string(column)
    if default is not None:
        pieces.append(f"DEFAULT {default}")
    forced_nullable = not column.nullable and default is None
    if not column.nullable and not forced_nullable:
        pieces.append("NOT NULL")
    return " ".join(pieces), forced_nullable


def model_indexes(table, dialect):
    quote = dialect.identifier_preparer.quote_identifier
    for index in sorted(table.indexes, key=lambda item: item.name):
        yield index.name, str(CreateIndex(index, if_not_exists=True).compile(dialect=dialect)), None
    for constraint in table.constraints:
        if not isinstance(constraint, UniqueConstraint):
            continue
        columns = tuple(column.name for column in constraint.columns)
        name = constraint.name or f"uq_{table.name}_{'_'.join(columns)}"
        fields = ", ".join(quote(column) for column in columns)
        yield name, f"CREATE UNIQUE INDEX IF NOT EXISTS {quote(name)} ON {quote(table.name)} ({fields})", columns
