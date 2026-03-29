import os
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_apscheduler import APScheduler
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

db = SQLAlchemy()
migrate = Migrate()
scheduler = APScheduler()
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=(os.getenv("RATELIMIT_STORAGE_URI", "memory://").strip() or "memory://"),
)
