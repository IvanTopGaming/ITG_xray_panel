import datetime
import io
import re
from urllib.parse import urlparse

import qrcode
from aiogram.types import BufferedInputFile


def generate_qr(data):
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    bio = io.BytesIO()
    img.save(bio)
    bio.seek(0)

    return BufferedInputFile(bio.getvalue(), filename="qrcode.png")


def format_bytes(size):
    power = 2**10
    n = 0
    power_labels = {0: "", 1: "K", 2: "M", 3: "G", 4: "T"}
    while size >= power and n < max(power_labels):
        size /= power
        n += 1
    return f"{size:.2f} {power_labels[n]}B"


_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9-]+")


def sanitize_filename_component(value, fallback="panel"):
    if value is None:
        return fallback
    normalized = _FILENAME_SAFE_RE.sub("_", str(value)).strip("_")
    return normalized or fallback


def _today_iso():
    return datetime.date.today().isoformat()


def bot_backup_filename(date_str=None):
    return f"bot_backup_{date_str or _today_iso()}.db"


def panel_backup_filename(panel, date_str=None):
    name = sanitize_filename_component(getattr(panel, "name", None))
    if name == "panel":
        host = urlparse(getattr(panel, "base_url", "") or "").hostname
        if host:
            name = sanitize_filename_component(host)
    return f"panel_{name}_{date_str or _today_iso()}.db"
