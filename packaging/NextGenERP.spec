# -*- mode: python ; coding: utf-8 -*-
# Build on Windows 10/11:
#   packaging\build_windows.bat
# Produces: dist\NextGenERP\NextGenERP.exe  (no Python required on customer PCs)

from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_all

block_cipher = None
ROOT = Path(SPECPATH).resolve().parent

datas = [(str(ROOT / "assets"), "assets")]
binaries = []
hidden = []

for pkg in ("PySide6", "shiboken6", "reportlab"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hidden += h
    except Exception:
        pass

hidden += collect_submodules("reportlab.graphics.barcode")
hidden += [
    "printing", "backup_service", "secret_store", "session_guard", "app_logging",
    "operations", "document_templates", "erp_dashboard", "fx", "pos_ui", "profiles",
    "branding", "command_palette", "insights", "pulse_ui", "reports", "returns_ui",
    "shifts", "hardware_profiles", "payment_providers", "paystack_client",
    "website_catalog", "website_catalog_ui", "workspace_isolation", "branch_sync",
    "advanced_features", "custom_module_schema", "etims_agent", "pos_config",
    "service_catalog", "operations_ui",
]

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "unittest"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NextGenERP",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="NextGenERP",
)
