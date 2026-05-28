import io

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
