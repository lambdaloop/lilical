from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)


def _project_root() -> Path:
    import os
    import sys

    # Inside a PyInstaller bundle all data files land in sys._MEIPASS.
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)  # type: ignore[reportAttributeAccessIssue]
    # Inside a Flatpak sandbox alembic.ini and migrations/ are installed here.
    if os.environ.get("FLATPAK_ID"):
        return Path("/app/share/lilical")
    return Path(__file__).resolve().parent.parent.parent.parent


def open_engine(db_path: str) -> Engine:
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")  # type: ignore[reportUntypedFunctionDecorator]
    def _pragmas(conn, _) -> None:  # type: ignore[reportUnusedFunction]
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    return engine


def ensure_schema(engine: Engine) -> None:
    import sys
    from alembic import command
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory

    root = _project_root()
    ini_path = root / "alembic.ini"
    cfg = AlembicConfig(str(ini_path))
    cfg.set_main_option("sqlalchemy.url", str(engine.url))
    # Resolve the migrations directory to an absolute path so alembic doesn't
    # fall back to CWD resolution (matters inside a PyInstaller bundle).
    cfg.set_main_option("script_location", str(root / "migrations"))

    if hasattr(sys, "_MEIPASS"):
        versions_dir = root / "migrations" / "versions"
        py_files = list(versions_dir.glob("*.py")) if versions_dir.exists() else []
        sd = ScriptDirectory.from_config(cfg)
        known = [r.revision for r in sd.walk_revisions()]
        log.info(
            "PyInstaller bundle: _MEIPASS=%s versions_dir_exists=%s py_count=%d alembic_revisions=%s",
            sys._MEIPASS,  # type: ignore[reportAttributeAccessIssue]
            versions_dir.exists(),
            len(py_files),
            known,
        )

    inspector = inspect(engine)
    if "settings" not in inspector.get_table_names():
        log.info("Fresh database — running initial schema migration")

    command.upgrade(cfg, "head")
