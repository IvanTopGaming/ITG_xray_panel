import argparse
import logging

from panel_core.split_monolith import (
    MigrationRefused,
    copy_for_migration,
    require_migrated,
    split,
)


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logger = logging.getLogger("split-monolith")

    parser = argparse.ArgumentParser(
        description=(
            "Split a 2.x monolith database into the master's Postgres and one node's SQLite. "
            "Run migrate_db.py against the source first; this refuses anything below the current schema."
        )
    )
    parser.add_argument("--sqlite", required=True, help="the monolith's panel.db")
    parser.add_argument("--pg", required=True, help="the shared Postgres URL")
    parser.add_argument("--node-out", required=True, help="where to write the node's panel.db")
    parser.add_argument(
        "--workdir",
        help="take a WAL-consistent copy of --sqlite here first, and split that instead",
    )
    parser.add_argument("--force", action="store_true", help="overwrite a non-empty target")
    args = parser.parse_args(argv)

    source = args.sqlite
    try:
        if args.workdir:
            source = copy_for_migration(args.sqlite, args.workdir)
            logger.info("copied %s to %s (WAL included)", args.sqlite, source)

        logger.info("source schema version: %s", require_migrated(source))
        report = split(source, args.pg, args.node_out, force=args.force)
    except MigrationRefused as exc:
        logger.error("%s", exc)
        return 1

    for side in ("master", "node"):
        logger.info("--- %s ---", side)
        for name in sorted(report[side]):
            if report[side][name]:
                logger.info("  %-24s %s", name, report[side][name])

    logger.info("node database written to %s", args.node_out)
    logger.info("next: link it from the new master, then point every tariff item with no panel_id at it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
