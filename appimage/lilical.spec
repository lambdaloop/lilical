# PyInstaller spec for lilical AppImage (onedir mode)
import sys
from pathlib import Path

root = Path(SPECPATH).parent  # appimage/ -> project root

# conda-forge installs QtWebEngineProcess and resources outside PySide6's tree;
# PyInstaller's hook expects the PyPI wheel layout so we bundle them explicitly.
_pixi_env = Path(sys.executable).resolve().parent.parent

a = Analysis(
    [str(root / "src" / "lilical" / "__main__.py")],
    pathex=[str(root / "src")],
    binaries=[
        (str(_pixi_env / "bin" / "QtWebEngineProcess6"), "."),
    ],
    datas=[
        (str(root / "alembic.ini"), "."),
        (str(root / "migrations"), "migrations"),
        (str(root / "src" / "lilical" / "ui" / "styles"), "lilical/ui/styles"),
        (str(_pixi_env / "share" / "qt6" / "resources"), "resources"),
        (str(_pixi_env / "share" / "qt6" / "translations" / "qtwebengine_locales"),
         "translations/qtwebengine_locales"),
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
