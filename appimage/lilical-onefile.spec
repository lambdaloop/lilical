# PyInstaller spec for lilical standalone onefile binary.
# Produces a single self-extracting executable; no AppImage wrapper needed.
# Relies on the host system's fontconfig — does not bundle fonts.
# Trade-off: ~1-2s extraction overhead on cold start vs. the onedir AppImage build.
import sys
from pathlib import Path

root = Path(SPECPATH).parent  # appimage/ -> project root
_pixi_lib = Path(sys.executable).resolve().parent.parent / "lib"

a = Analysis(
    [str(root / "src" / "lilical" / "__main__.py")],
    pathex=[str(root / "src")],
    binaries=[
        # libglvnd dispatch layer — same rationale as onedir spec.
        # Minimal installs (CI runners, containers) don't ship these.
        (str(_pixi_lib / "libOpenGL.so.0"), "."),
        (str(_pixi_lib / "libEGL.so.1"), "."),
        (str(_pixi_lib / "libGL.so.1"), "."),
        (str(_pixi_lib / "libGLX.so.0"), "."),
        (str(_pixi_lib / "libGLdispatch.so.0"), "."),
        # Conda-forge libssl/libcrypto — without these, _ssl.so has unresolved
        # symbol versions and `import ssl` aborts at runtime.
        (str(_pixi_lib / "libssl.so.3"), "."),
        (str(_pixi_lib / "libcrypto.so.3"), "."),
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
        # zoneinfo falls back to tzdata when the conda TZPATH is absent
        "tzdata",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=["runtime_hooks/setup_env.py"],
    excludes=[
        "Qt3DAnimation", "Qt3DCore", "Qt3DExtras", "Qt3DInput", "Qt3DLogic", "Qt3DRender",
        "QtCharts", "QtDataVisualization",
        "QtPdf", "QtPdfWidgets",
        "QtQuick", "QtQml", "QtQmlWorkerScript",
        "QtWebEngineCore", "QtWebEngineWidgets", "QtWebEngineQuick", "QtWebChannel",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="lilical",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
