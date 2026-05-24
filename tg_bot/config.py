"""Bot bootstrap environment.

Everything except these three vars now lives in the panel DB (SystemSetting)
and is fetched by `runtime_config.RuntimeConfig`:

  BACKEND_API_URL    — required, e.g. http://backend:5000/api
  BOT_SERVICE_TOKEN  — required, set via panel "Rotate token" button
  BOT_LOG_LEVEL      — optional, defaults to INFO
"""

import logging
import os
import sys


BACKEND_API_URL = (os.environ.get("BACKEND_API_URL") or "").rstrip("/")
BOT_SERVICE_TOKEN = os.environ.get("BOT_SERVICE_TOKEN") or ""
BOT_LOG_LEVEL = (os.environ.get("BOT_LOG_LEVEL") or "INFO").upper()


if not BACKEND_API_URL:
    logging.error("BACKEND_API_URL env var is required (e.g. http://backend:5000/api).")
    sys.exit(1)

if not BOT_SERVICE_TOKEN:
    logging.error(
        "BOT_SERVICE_TOKEN env var is required. Generate one via the panel UI (Bot → Settings → Rotate token)."
    )
    sys.exit(1)
