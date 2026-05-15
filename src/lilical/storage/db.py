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
        return Path(sys._MEIPASS)
    # Inside a Flatpak sandbox alembic.ini and migrations/ are installed here.
    if os.environ.get("FLATPAK_ID"):
        return Path("/app/share/lilical")
    return Path(__file__).resolve().parent.parent.parent.parent


def open_engine(db_path: str) -> Engine:
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _pragmas(conn, _) -> None:  # type: ignore[reportUnusedFunction]
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    return engine


def ensure_schema(engine: Engine) -> None:
    from alembic import command
    from alembic.config import Config as AlembicConfig

    root = _project_root()
    ini_path = root / "alembic.ini"
    cfg = AlembicConfig(str(ini_path))
    cfg.set_main_option("sqlalchemy.url", str(engine.url))
    # Resolve the migrations directory to an absolute path so alembic doesn't
    # fall back to CWD resolution (matters inside a PyInstaller bundle).
    cfg.set_main_option("script_location", str(root / "migrations"))

    inspector = inspect(engine)
    if "settings" not in inspector.get_table_names():
        log.info("Fresh database — running initial schema migration")

    command.upgrade(cfg, "head")
