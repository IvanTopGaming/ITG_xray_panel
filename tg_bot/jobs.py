import asyncio
import logging
import datetime
import time
import os
from aiogram import Bot
from aiogram.types import FSInputFile, BufferedInputFile
from config import BACKUP_GROUP_ID
from database import db
from api_service import panel_api
from utils import format_bytes

logger = logging.getLogger(__name__)


async def send_backup(bot: Bot):
    if not BACKUP_GROUP_ID:
        logger.warning("backup_group_id not set in config. Skipping backup sending.")
        return

    date_str = datetime.datetime.now().strftime("%Y-%m-%d")
    db_path = "db/bot.db"

    targets = [BACKUP_GROUP_ID]

    if os.path.exists(db_path):
        for target_id in targets:
            try:
                backup_file = FSInputFile(db_path, filename=f"bot_backup_{date_str}.db")
                await bot.send_document(
                    target_id,
                    backup_file,
                    caption=f"🤖 <b>Bot Backup</b>\n📅 {date_str}",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error(f"Bot backup send error to {target_id}: {e}")

    for p in panel_api.panels:
        try:
            content = await p.request("GET", "backup")
            if content and isinstance(content, bytes):
                safe_name = p.name.replace(" ", "_")
                for target_id in targets:
                    try:
                        input_file = BufferedInputFile(content, filename=f"panel_{safe_name}_{date_str}.db")
                        await bot.send_document(
                            target_id,
                            input_file,
                            caption=f"🎛 <b>{p.name}</b> Backup",
                            parse_mode="HTML",
                        )
                    except Exception as e:
                        logger.error(f"Panel backup send error to {target_id}: {e}")
        except Exception as e:
            logger.error(f"Panel backup download error {p.name}: {e}")


async def sync_users_across_panels(bot: Bot):
    users = db.get_all_users()
    if not users:
        return

    panel_clients = {}
    healthy_panels = set()
    for panel in panel_api.panels:
        try:
            inbounds = await panel.request("GET", "inbounds")
            if not isinstance(inbounds, list):
                panel_clients[panel.name] = {}
                continue

            existing_clients = {}
            for ib in inbounds:
                inbound_tag = str(ib.get("tag") or "").strip()
                if not inbound_tag:
                    continue
                if inbound_tag not in existing_clients:
                    existing_clients[inbound_tag] = {}
                for c in ib.get("settings", {}).get("clients", []):
                    email = str(c.get("email", "") or "").strip()
                    if email:
                        existing_clients[inbound_tag][email] = c
            panel_clients[panel.name] = existing_clients
            healthy_panels.add(panel.name)
        except Exception as e:
            logger.error(f"Sync error on {panel.name}: {e}")
            panel_clients[panel.name] = {}

    reference_clients = {}
    reference_clients_by_email = {}
    for clients_map in panel_clients.values():
        for inbound_tag, inbound_clients in clients_map.items():
            for email, client_data in inbound_clients.items():
                key = (inbound_tag, email)
                if key not in reference_clients and isinstance(client_data, dict):
                    reference_clients[key] = client_data
                if email not in reference_clients_by_email and isinstance(client_data, dict):
                    reference_clients_by_email[email] = client_data

    for user in users:
        email = str(user[3] or "").strip()
        if not email:
            continue
        inbound_tag = str(user[4] or "").strip()
        if inbound_tag.lower() == "multi":
            inbound_tag = ""

        db_user_id = str(user[5] or "").strip()
        reference = (
            reference_clients.get((inbound_tag, email), {})
            if inbound_tag
            else reference_clients_by_email.get(email, {})
        )

        try:
            limit_bytes = int(reference.get("limit_bytes", 0) or 0)
        except (TypeError, ValueError, AttributeError):
            limit_bytes = 0
        try:
            expiry_time = int(reference.get("expiry_time", 0) or 0)
        except (TypeError, ValueError, AttributeError):
            expiry_time = 0
        if isinstance(reference, dict):
            raw_enable = reference.get("enable", True)
            if isinstance(raw_enable, str):
                enabled = raw_enable.strip().lower() in ["1", "true", "yes", "on"]
            else:
                enabled = bool(raw_enable)
        else:
            enabled = True

        user_id = db_user_id or str(reference.get("id", "") or "").strip()

        for panel in panel_api.panels:
            if panel.name not in healthy_panels:
                continue
            target_inbound = inbound_tag or panel.target_inbound
            panel_existing = panel_clients.get(panel.name, {})
            existing = panel_existing.get(target_inbound, {})
            if email in existing:
                continue

            payload = {
                "email": email,
                "limit_bytes": limit_bytes,
                "expiry_time": expiry_time,
                "enable": enabled,
            }
            if user_id:
                payload["id"] = user_id

            logger.info(f"Healing {email} on {panel.name}")
            res = await panel.request(
                "POST",
                f"inbounds/{target_inbound}/users",
                json_data=payload,
            )
            if isinstance(res, dict) and "id" in res:
                panel_clients.setdefault(panel.name, {}).setdefault(target_inbound, {})[email] = res
                if not user_id:
                    user_id = str(res.get("id") or "").strip()
            else:
                reason = res.get("error") if isinstance(res, dict) else "unknown error"
                logger.warning(f"Heal failed for {email} on {panel.name}: {reason}")


async def check_and_notify_users(bot: Bot):
    users = db.get_all_users()
    for user in users:
        tg_id = user[1]
        email = user[3]
        inbound_tag = user[4]
        try:
            stats = await panel_api.get_client_stats_aggregate(email, inbound_tag=inbound_tag)
            if not stats:
                continue
            if not stats["enable"]:
                continue
            if stats["expiry"] > 0:
                now_ts = int(time.time() * 1000)
                diff = stats["expiry"] - now_ts
                days_left = diff / (1000 * 60 * 60 * 24)
                if 0 < days_left <= 3:
                    days_int = int(days_left) + 1
                    await bot.send_message(
                        tg_id,
                        f"⏳ Subscription for <b>{email}</b> ends in <b>{days_int} days</b>.",
                        parse_mode="HTML",
                    )
            if stats["limit"] > 0:
                used = stats["total"]
                limit = stats["limit"]
                percent_used = (used / limit) * 100
                if percent_used >= 90 and percent_used < 100:
                    left = limit - used
                    await bot.send_message(
                        tg_id,
                        f"⚠️ <b>Traffic Warning</b>\nUsed: {percent_used:.1f}%\nRemaining: {format_bytes(left)}",
                        parse_mode="HTML",
                    )
            await asyncio.sleep(0.1)
        except Exception as e:
            logger.error(f"Check error {email}: {e}")
