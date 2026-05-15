# PyInstaller spec for lilical AppImage (onedir mode)
from pathlib import Path

root = Path(SPECPATH).parent  # appimage/ -> project root

a = Analysis(
    [str(root / "src" / "lilical" / "__main__.py")],
    pathex=[str(root / "src")],
    binaries=[],
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
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Strip large unused Qt modules to save space
        "Qt3DAnimation", "Qt3DCore", "Qt3DExtras", "Qt3DInput", "Qt3DLogic", "Qt3DRender",
        "QtCharts", "QtDataVisualization",
        "QtWebEngineCore", "QtWebEngineWidgets", "QtWebEngineQuick",
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
