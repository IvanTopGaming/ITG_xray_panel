import argparse
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from flask import Flask

from panel_core.db_migration import migrate_sqlite_db
from panel_core.extensions import db
from panel_core.services.shared_client_migration import apply_migration, migration_report


def main():
    parser = argparse.ArgumentParser(
        description="Inspect or migrate an offline SQLite copy without touching the source"
    )
    parser.add_argument("database", type=Path)
    parser.add_argument("--apply-to", type=Path)
    args = parser.parse_args()
    source = args.database.resolve(strict=True)
    if args.apply_to and args.apply_to.exists():
        parser.error("--apply-to must name a new output file")
    with tempfile.TemporaryDirectory(prefix="shared-client-migration-") as directory:
        candidate = Path(directory) / "panel.db"
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as reader, sqlite3.connect(candidate) as writer:
            reader.backup(writer)
        migrate_sqlite_db(str(candidate), seed_bot_texts=False)
        app = Flask(__name__)
        app.config.update(
            SQLALCHEMY_DATABASE_URI=f"sqlite:///{candidate}",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            XRAY_CONFIG_LOCK_PATH=str(Path(directory) / "runtime.lock"),
        )
        db.init_app(app)
        with app.app_context():
            report = migration_report()
            if args.apply_to:
                result = apply_migration(report)
                db.session.remove()
                db.engine.dispose()
                descriptor = os.open(args.apply_to, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(descriptor)
                try:
                    with sqlite3.connect(candidate) as reader, sqlite3.connect(args.apply_to) as writer:
                        reader.backup(writer)
                except Exception:
                    args.apply_to.unlink(missing_ok=True)
                    raise
                report["result"] = result
            print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
