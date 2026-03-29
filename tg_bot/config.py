import yaml
import os
import sys
import logging
from urllib.parse import urlparse

CONFIG_PATH = "/app/config.yaml"


def load_config():
    if not os.path.exists(CONFIG_PATH):
        logging.error(f"Config file not found at {CONFIG_PATH}")
        sys.exit(1)

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            parsed = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        logging.error("Invalid YAML in %s: %s", CONFIG_PATH, exc)
        sys.exit(1)

    if not isinstance(parsed, dict):
        logging.error("Config must be a YAML object at top level")
        sys.exit(1)
    return parsed


def _normalize_admin_ids(raw_admin_ids):
    if raw_admin_ids is None:
        return []
    if not isinstance(raw_admin_ids, list):
        logging.error("admin_ids must be an array")
        sys.exit(1)

    result = []
    for item in raw_admin_ids:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            logging.error("Invalid admin_ids entry: %s", item)
            sys.exit(1)
    return result


def _validate_server_config(raw_servers):
    if not isinstance(raw_servers, list) or not raw_servers:
        logging.error("servers configuration must be a non-empty array")
        sys.exit(1)

    normalized = []
    required_fields = ("name", "url", "user", "password", "inbound_tag")
    for index, item in enumerate(raw_servers, start=1):
        if not isinstance(item, dict):
            logging.error("servers[%s] must be an object", index)
            sys.exit(1)

        missing = [field for field in required_fields if not str(item.get(field) or "").strip()]
        if missing:
            logging.error("servers[%s] missing required fields: %s", index, ", ".join(missing))
            sys.exit(1)

        url = str(item["url"]).strip()
        parsed_url = urlparse(url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            logging.error("servers[%s].url must be a valid http(s) URL", index)
            sys.exit(1)

        normalized.append(
            {
                "name": str(item["name"]).strip(),
                "url": url.rstrip("/"),
                "user": str(item["user"]).strip(),
                "password": str(item["password"]),
                "inbound_tag": str(item["inbound_tag"]).strip(),
            }
        )

    return normalized


cfg = load_config()

BOT_TOKEN = str(cfg.get("bot_token") or "").strip()
ADMIN_IDS = _normalize_admin_ids(cfg.get("admin_ids"))
BACKUP_GROUP_ID = cfg.get("backup_group_id")
SERVERS_CONFIG = _validate_server_config(cfg.get("servers"))

if not BOT_TOKEN:
    logging.error("bot_token is required.")
    sys.exit(1)
