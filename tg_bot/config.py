import logging
import os
import sys


BACKEND_API_URL = (os.environ.get("BACKEND_API_URL") or "").rstrip("/")
BOT_SERVICE_TOKEN = os.environ.get("BOT_SERVICE_TOKEN") or ""
BOT_LOG_LEVEL = (os.environ.get("BOT_LOG_LEVEL") or "INFO").upper()


if not BACKEND_API_URL:
    logging.error("BACKEND_API_URL env var is required (e.g. http://backend:5000/api).")
    sys.exit(1)
