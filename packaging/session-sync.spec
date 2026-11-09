# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec: builds "Session Sync.app" (onedir, no terminal window).
import sys
from pathlib import Path

PACKAGING = Path(SPECPATH)
PROJECT = PACKAGING.parent
SRC = PROJECT / "src"
sys.path.insert(0, str(SRC))

from session_sync import __version__  # noqa: E402

ICON = PROJECT / "build" / "AppIcon.icns"

a = Analysis(
    [str(SRC / "session_sync" / "app.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="session-sync",
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
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="session-sync",
)

app = BUNDLE(
    coll,
    name="Session Sync.app",
    icon=str(ICON) if ICON.exists() else None,
    bundle_identifier="dev.session-sync.app",
    version=__version__,
    info_plist={
        "CFBundleDisplayName": "Session Sync",
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication",
        "LSMinimumSystemVersion": "12.0",
        "LSApplicationCategoryType": "public.app-category.developer-tools",
    },
)
