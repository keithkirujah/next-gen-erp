"""Protect secrets (e.g. the GitHub backup token) so they are not readable in the database file.

On Windows the value is encrypted with DPAPI (``CryptProtectData``): only the same Windows user
account on the same computer can decrypt it, and nothing extra needs to be installed. A copy of the
database or a backup ZIP therefore does NOT carry a usable token.

On other systems (development/testing only) the value is stored with a ``plain:`` marker.
"""
from __future__ import annotations

import base64
import sys

_PREFIX_DPAPI = "dpapi:"
_PREFIX_PLAIN = "plain:"


def is_protected(stored: str) -> bool:
    return str(stored or "").startswith(_PREFIX_DPAPI)


def protect(secret: str) -> str:
    secret = str(secret or "")
    if not secret:
        return ""
    if sys.platform == "win32":
        try:
            return _PREFIX_DPAPI + base64.b64encode(_dpapi(secret.encode("utf-8"), encrypt=True)).decode("ascii")
        except Exception:  # fall through: better stored plainly than silently lost
            pass
    return _PREFIX_PLAIN + base64.b64encode(secret.encode("utf-8")).decode("ascii")


def unprotect(stored: str) -> str:
    stored = str(stored or "")
    if not stored:
        return ""
    try:
        if stored.startswith(_PREFIX_DPAPI):
            return _dpapi(base64.b64decode(stored[len(_PREFIX_DPAPI):]), encrypt=False).decode("utf-8")
        if stored.startswith(_PREFIX_PLAIN):
            return base64.b64decode(stored[len(_PREFIX_PLAIN):]).decode("utf-8")
    except Exception:
        return ""
    return stored  # legacy value saved before protection existed


def _dpapi(data: bytes, encrypt: bool) -> bytes:  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    description = "NextGen ERP secret"
    if encrypt:
        ok = crypt32.CryptProtectData(ctypes.byref(blob_in), description, None, None, None, 0, ctypes.byref(blob_out))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)
