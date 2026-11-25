from io import BytesIO

import qrcode
from qrcode.exceptions import DataOverflowError


def DEF_CREATE_QRCODE(MESSAGE_TEXT):
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=10, border=4)
    qr.add_data(MESSAGE_TEXT)
    try:
        qr.make(fit=True)
    except (DataOverflowError, ValueError):
        raise ValueError("This text is too long for a QR code. Send a shorter text or URL.") from None
    qr_img = qr.make_image(fill_color=(0, 59, 117), back_color="white")
    output = BytesIO()
    output.name = "pg-toolbox-qr.png"
    qr_img.save(output, "PNG")
    output.seek(0)
    return output
