"""One-shot: legacy standalone bot SQLite → panel.db (telegram_id link + canonical rename).

Matches Client by UUID → old email → already-renamed pattern (idempotent).
Renames email to `tg<id>_<tag>` or `tg<id>_<tag>__<old>` for shared keys
(multiple keys on same inbound for one tg_id). Subscription URLs are
UUID-keyed, so renames don't break user-side configs.

Usage:
  python -m scripts.migrate_to_billing PANEL_DB BOT_DB
  BOT_DB_PATH=/app/bot_db/bot.db python -m scripts.migrate_to_billing PANEL_DB
"""

from __future__ import annotations

import os
import sqlite3
import sys
from collections import Counter
from typing import Dict


def migrate(panel_db_path: str, bot_db_path: str) -> Dict[str, int]:
    if not os.path.exists(bot_db_path):
        print(f"migrate_to_billing: legacy DB not found at {bot_db_path} — nothing to do.")
        return {"linked": 0, "telegram_users": 0, "orphaned": 0}

    panel = sqlite3.connect(panel_db_path)
    bot = sqlite3.connect(bot_db_path)

    try:
        bot_users = bot.execute("SELECT telegram_id, username, panel_email, uuid FROM users").fetchall()
    except sqlite3.OperationalError as exc:
        print(f"migrate_to_billing: cannot read users from {bot_db_path}: {exc}", file=sys.stderr)
        return {"linked": 0, "telegram_users": 0, "orphaned": -1}

    tg_counts = Counter(r[0] for r in bot_users)

    linked = 0
    renamed = []
    orphaned = []
    tg_seen: Dict[int, str | None] = {}

    for tg_id, username, old_email, uuid in bot_users:
        # Three-stage lookup: UUID → old email → already-renamed pattern (idempotent re-runs).
        row = panel.execute(
            "SELECT id, email, inbound_tag, telegram_id FROM client WHERE id=?",
            (uuid,),
        ).fetchone()
        if not row:
            row = panel.execute(
                "SELECT id, email, inbound_tag, telegram_id FROM client WHERE email=?",
                (old_email,),
            ).fetchone()
        if not row:
            pattern = f"tg{tg_id}_%__{old_email}" if tg_counts[tg_id] > 1 else f"tg{tg_id}_%"
            row = panel.execute(
                "SELECT id, email, inbound_tag, telegram_id FROM client WHERE telegram_id=? AND email LIKE ?",
                (tg_id, pattern),
            ).fetchone()
        if not row:
            orphaned.append((tg_id, old_email, uuid))
            print(f"  orphan: tg={tg_id} email={old_email!r} uuid={uuid!r} — no matching Client")
            continue

        cid, cur_email, inbound_tag, existing_tg = row
        if existing_tg == tg_id and cur_email.startswith(f"tg{tg_id}_"):
            # Already migrated — skip silently.
            if tg_id not in tg_seen:
                tg_seen[tg_id] = username
            continue

        if tg_counts[tg_id] == 1:
            new_email = f"tg{tg_id}_{inbound_tag}"
        else:
            new_email = f"tg{tg_id}_{inbound_tag}__{old_email}"

        panel.execute(
            "UPDATE client SET telegram_id=?, email=? WHERE id=?",
            (tg_id, new_email, cid),
        )
        panel.execute(
            "UPDATE node_client_traffic SET email=? WHERE email=?",
            (new_email, cur_email),
        )
        renamed.append((cur_email, new_email, tg_id))
        linked += 1
        if tg_id not in tg_seen:
            tg_seen[tg_id] = username

    inserted = 0
    for tg_id, username in tg_seen.items():
        cur = panel.execute(
            "INSERT OR IGNORE INTO telegram_user "
            "(telegram_id, username, language, language_chosen, blocked) "
            "VALUES (?, ?, 'ru', 0, 0)",
            (tg_id, username),
        )
        inserted += cur.rowcount

    panel.commit()
    panel.close()
    bot.close()

    print(f"\nLinked / renamed clients: {linked}")
    print(f"telegram_user inserted:   {inserted}")
    print(f"Orphaned (no Client):     {len(orphaned)}")
    if renamed:
        print("\nRenames (sample):")
        for old, new, tg in renamed[:10]:
            print(f"  tg={tg:<12} {old!r} -> {new!r}")

    return {
        "linked": linked,
        "telegram_users": inserted,
        "orphaned": len(orphaned),
    }


def main() -> None:
    argv = sys.argv[1:]
    if len(argv) == 2:
        panel_db, bot_db = argv
    elif len(argv) == 1:
        panel_db = argv[0]
        bot_db = os.environ.get("BOT_DB_PATH", "/app/bot_db/bot.db")
    else:
        print(__doc__)
        sys.exit(2)

    result = migrate(panel_db, bot_db)
    sys.exit(0 if result["orphaned"] == 0 else 1)


if __name__ == "__main__":
    main()
