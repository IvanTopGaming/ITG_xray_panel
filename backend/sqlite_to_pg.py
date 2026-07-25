from sqlalchemy import create_engine, func, insert, select, text

from panel_core.extensions import db
import panel_core.models  # noqa: F401


def _drop_foreign_keys(engine):
    dropped = 0
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT conrelid::regclass AS tbl, conname FROM pg_constraint WHERE contype = 'f'")
        ).fetchall()
        for tbl, conname in rows:
            conn.execute(text('ALTER TABLE {} DROP CONSTRAINT IF EXISTS "{}"'.format(tbl, conname)))
            dropped += 1
    return dropped


def _reset_sequences(conn, metadata):
    for table in metadata.sorted_tables:
        for col in table.primary_key.columns:
            seq = conn.execute(text("SELECT pg_get_serial_sequence(:t, :c)"), {"t": table.name, "c": col.name}).scalar()
            if not seq:
                continue
            max_id = conn.execute(text("SELECT max({}) FROM {}".format(col.name, table.name))).scalar()
            if max_id is not None:
                conn.execute(text("SELECT setval(:s, :m)"), {"s": seq, "m": max_id})


def import_sqlite_to_pg(sqlite_path, pg_url, force=False):
    src = create_engine("sqlite:///{}".format(sqlite_path))
    dst = create_engine(pg_url)
    md = db.metadata
    md.create_all(dst)
    _drop_foreign_keys(dst)

    counts = {}
    with src.connect() as sconn, dst.begin() as dconn:
        if not force:
            for table in md.sorted_tables:
                n = dconn.execute(select(func.count()).select_from(table)).scalar()
                if n:
                    raise RuntimeError(
                        "target table {} is not empty ({} rows); pass force=True to override".format(table.name, n)
                    )
        for table in md.sorted_tables:
            rows = [dict(r._mapping) for r in sconn.execute(select(table))]
            if rows:
                dconn.execute(insert(table), rows)
            counts[table.name] = len(rows)
        _reset_sequences(dconn, md)

    src.dispose()
    dst.dispose()
    return counts


def verify_counts(sqlite_path, pg_url):
    src = create_engine("sqlite:///{}".format(sqlite_path))
    dst = create_engine(pg_url)
    md = db.metadata
    mismatches = []
    with src.connect() as sconn, dst.connect() as dconn:
        for table in md.sorted_tables:
            s = sconn.execute(select(func.count()).select_from(table)).scalar()
            d = dconn.execute(select(func.count()).select_from(table)).scalar()
            if s != d:
                mismatches.append((table.name, s, d))
    src.dispose()
    dst.dispose()
    return mismatches


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="Import a v21 panel.db into Postgres")
    parser.add_argument("--sqlite", required=True)
    parser.add_argument("--pg", required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    counts = import_sqlite_to_pg(args.sqlite, args.pg, force=args.force)
    for name in sorted(counts):
        print("{:32s} {}".format(name, counts[name]))
    mismatches = verify_counts(args.sqlite, args.pg)
    if mismatches:
        for name, s, d in mismatches:
            print("MISMATCH {}: sqlite={} pg={}".format(name, s, d))
        return 1
    print("OK: all table counts match")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
