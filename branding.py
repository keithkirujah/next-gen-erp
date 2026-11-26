"""Per-business branding: logo import (any common image) -> small PNG stored with the business data."""
from __future__ import annotations

import base64

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QImage

MAX_W, MAX_H = 480, 240          # plenty for A4 letterheads and receipts, keeps backups small
MAX_BYTES = 200_000


def load_logo_b64(path: str) -> str:
    image = QImage(str(path))
    if image.isNull():
        raise ValueError("That file could not be read as an image. Use PNG, JPG, BMP, GIF or WEBP.")
    if image.width() > MAX_W or image.height() > MAX_H:
        image = image.scaled(MAX_W, MAX_H, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    while True:
        data = QByteArray()
        buf = QBuffer(data)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buf, "PNG")
        buf.close()
        if len(data) <= MAX_BYTES or image.width() < 80:
            break
        image = image.scaled(int(image.width() * 0.75), int(image.height() * 0.75), Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    return base64.b64encode(bytes(data)).decode("ascii")


def logo_data_uri(b64: str) -> str:
    return "data:image/png;base64," + b64 if b64 else ""
