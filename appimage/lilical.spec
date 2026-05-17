# PyInstaller spec for lilical AppImage (onedir mode)
import sys
from pathlib import Path

root = Path(SPECPATH).parent  # appimage/ -> project root
_pixi_lib = Path(sys.executable).resolve().parent.parent / "lib"

a = Analysis(
    [str(root / "src" / "lilical" / "__main__.py")],
    pathex=[str(root / "src")],
    binaries=[
        # libglvnd dispatch layer — PyInstaller excludes these as "system libs"
        # but minimal installs (CI runners, containers) don't have them.
        # Safe to bundle: these sit above the GPU driver, not below it.
        (str(_pixi_lib / "libOpenGL.so.0"), "."),
        (str(_pixi_lib / "libEGL.so.1"), "."),
        (str(_pixi_lib / "libGL.so.1"), "."),
        (str(_pixi_lib / "libGLX.so.0"), "."),
        (str(_pixi_lib / "libGLdispatch.so.0"), "."),
    ],
    datas=[
        (str(root / "alembic.ini"), "."),
        (str(root / "migrations"), "migrations"),
        (str(root / "src" / "lilical" / "ui" / "styles"), "lilical/ui/styles"),
    ],
    hiddenimports=[
        # All backends are imported dynamically via factory.py
        "lilical.backends.google",
        "lilical.backends.graph",
        "lilical.backends.caldav",
        "lilical.backends.base",
        "lilical.backends.factory",
        "lilical.backends._google_serializer",
        "lilical.backends._ical_serializer",
        # SQLAlchemy loads dialects lazily
        "sqlalchemy.dialects.sqlite",
        # Keyring picks a backend at runtime
        "keyring.backends.SecretService",
        "keyring.backends.fail",
        "keyring.backend",
        # secretstorage / jeepney for D-Bus secrets
        "secretstorage",
        "jeepney",
        "jeepney.io.blocking",
        # desktop-notifier loads a backend per platform
        "desktop_notifier.backends.linux",
        # alembic runtime migration needs explicit import
        "alembic.runtime.migration",
        "alembic.operations.ops",
        # zoneinfo falls back to tzdata when the conda TZPATH is absent;
        # icalendar constructs ZoneInfo("UTC") at import time
        "tzdata",
        # WebEngine is imported lazily in account_setup._run_embedded_oauth
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtNetwork",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Strip large unused Qt modules to save space
        "Qt3DAnimation", "Qt3DCore", "Qt3DExtras", "Qt3DInput", "Qt3DLogic", "Qt3DRender",
        "QtCharts", "QtDataVisualization",
        "QtPdf", "QtPdfWidgets",
        "QtQuick", "QtQml", "QtQmlWorkerScript",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="lilical",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="lilical",
)
