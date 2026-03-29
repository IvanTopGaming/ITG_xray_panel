import logging
import os

from db_migration import migrate_sqlite_db


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logger = logging.getLogger("db-migrate")

    default_db_path = os.path.join(os.getcwd(), "db", "panel.db")
    db_path = str(os.getenv("PANEL_DB_PATH", default_db_path) or "").strip()
    if not db_path:
        logger.error("PANEL_DB_PATH is empty")
        return 1

    report = migrate_sqlite_db(db_path, logger=logger)
    logger.info(
        "Migration report: %s",
        report,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
