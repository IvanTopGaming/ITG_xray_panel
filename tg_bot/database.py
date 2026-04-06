import sqlite3
import os
import logging


class BotDB:
    def __init__(self, db_file="db/bot.db"):
        os.makedirs(os.path.dirname(db_file), exist_ok=True)
        self.db_file = db_file
        self.init_db()

    def get_connection(self):
        return sqlite3.connect(self.db_file)

    def init_db(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA synchronous=NORMAL;")

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    username TEXT,
                    panel_email TEXT NOT NULL,
                    inbound_tag TEXT NOT NULL,
                    uuid TEXT NOT NULL
                )
            """
            )
            self._migrate_users_table(cursor)

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS user_errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    server_name TEXT NOT NULL,
                    error_message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(telegram_id, server_name)
                )
            """
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    email TEXT NOT NULL,
                    notification_type TEXT NOT NULL,
                    sent_date TEXT NOT NULL,
                    UNIQUE(telegram_id, email, notification_type, sent_date)
                )
            """
            )

            self._deduplicate_users(cursor)
            cursor.execute("DROP INDEX IF EXISTS idx_users_panel_email")
            cursor.execute("DROP INDEX IF EXISTS idx_users_uuid")
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_panel_email_inbound ON users(panel_email, inbound_tag)"
            )
            cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_uuid_inbound ON users(uuid, inbound_tag)")
            conn.commit()

    def _migrate_users_table(self, cursor):
        cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'")
        row = cursor.fetchone()
        sql = str(row[0] or "") if row else ""
        needs_rebuild = "UNIQUE(panel_email)" in sql or "UNIQUE(uuid)" in sql
        if not needs_rebuild:
            return

        cursor.execute("DROP TABLE IF EXISTS users_migrated")
        cursor.execute(
            """
            CREATE TABLE users_migrated (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                username TEXT,
                panel_email TEXT NOT NULL,
                inbound_tag TEXT NOT NULL,
                uuid TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            INSERT INTO users_migrated (id, telegram_id, username, panel_email, inbound_tag, uuid)
            SELECT
                id,
                telegram_id,
                username,
                panel_email,
                COALESCE(NULLIF(inbound_tag, ''), 'multi'),
                uuid
            FROM users
            """
        )
        cursor.execute("DROP TABLE users")
        cursor.execute("ALTER TABLE users_migrated RENAME TO users")

    def _deduplicate_users(self, cursor):
        cursor.execute(
            """
            DELETE FROM users
            WHERE id NOT IN (
                SELECT MAX(id) FROM users GROUP BY panel_email, inbound_tag
            )
            """
        )
        cursor.execute(
            """
            DELETE FROM users
            WHERE id NOT IN (
                SELECT MAX(id) FROM users GROUP BY uuid, inbound_tag
            )
            """
        )

    def add_user(self, telegram_id, username, panel_email, inbound_tag, uuid):
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO users (telegram_id, username, panel_email, inbound_tag, uuid)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(panel_email, inbound_tag) DO UPDATE SET
                        telegram_id = excluded.telegram_id,
                        username = COALESCE(excluded.username, users.username),
                        uuid = excluded.uuid
                    """,
                    (telegram_id, username, panel_email, inbound_tag, uuid),
                )
                conn.commit()
            return True
        except sqlite3.Error as e:
            logging.error(f"DB Error add_user: {e}")
            return False

    def get_user_by_db_id(self, db_id):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE id = ?", (db_id,))
            return cursor.fetchone()

    def get_users_by_tg_id(self, telegram_id):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
            return cursor.fetchall()

    def get_all_users(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users ORDER BY id DESC")
            return cursor.fetchall()

    def get_unique_users(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT telegram_id, username FROM users")
            return cursor.fetchall()

    def update_username(self, telegram_id, new_username):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET username = ? WHERE telegram_id = ?",
                (new_username, telegram_id),
            )
            conn.commit()

    def update_telegram_id_for_record(self, db_id, new_tg_id):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET telegram_id = ? WHERE id = ?",
                (new_tg_id, db_id),
            )
            conn.commit()
        return True

    def update_panel_email(self, db_id, new_email):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET panel_email = ? WHERE id = ?",
                (new_email, db_id),
            )
            conn.commit()

    def delete_user_record(self, db_id):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM users WHERE id = ?", (db_id,))
            conn.commit()

    def add_error(self, telegram_id, server_name, error_message):
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT OR REPLACE INTO user_errors (telegram_id, server_name, error_message)
                    VALUES (?, ?, ?)
                    """,
                    (telegram_id, server_name, error_message),
                )
                conn.commit()
        except Exception as e:
            logging.error(f"DB Error add_error: {e}")

    def get_errors(self, telegram_id):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT server_name, error_message FROM user_errors WHERE telegram_id = ?",
                (telegram_id,),
            )
            return cursor.fetchall()

    def remove_error(self, telegram_id, server_name):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM user_errors WHERE telegram_id = ? AND server_name = ?",
                (telegram_id, server_name),
            )
            conn.commit()

    def clear_errors(self, telegram_id):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM user_errors WHERE telegram_id = ?", (telegram_id,))
            conn.commit()

    def was_notified(self, telegram_id, email, notification_type, sent_date):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM notification_log WHERE telegram_id=? AND email=? AND notification_type=? AND sent_date=?",
                (telegram_id, email, notification_type, sent_date),
            )
            return cursor.fetchone() is not None

    def record_notification(self, telegram_id, email, notification_type, sent_date):
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT OR IGNORE INTO notification_log (telegram_id, email, notification_type, sent_date) VALUES (?, ?, ?, ?)",
                    (telegram_id, email, notification_type, sent_date),
                )
                conn.commit()
        except Exception as e:
            logging.error(f"DB Error record_notification: {e}")

    def prune_notification_log(self, days_to_keep=14):
        """Remove old notification entries to keep the table small."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "DELETE FROM notification_log WHERE sent_date < date('now', ?)",
                    (f"-{days_to_keep} days",),
                )
                conn.commit()
        except Exception as e:
            logging.error(f"DB Error prune_notification_log: {e}")


db = BotDB()
