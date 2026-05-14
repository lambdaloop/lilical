from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine


class SchemaOutOfDate(Exception):
    def __init__(self, expected: str, actual: str | None) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"Schema {actual} != expected {expected}. Run `pixi run migrate`.")


def open_engine(db_path: str) -> Engine:
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _pragmas(conn, _) -> None:
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    return engine


def ensure_schema(engine: Engine) -> None:
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory

    cfg = AlembicConfig("alembic.ini")
    script = ScriptDirectory.from_config(cfg)
    expected = script.get_current_head()
    with engine.connect() as conn:
        result = conn.exec_driver_sql(
            "SELECT value FROM settings WHERE key='schema_version'"
        ).scalar()
    if result != expected:
        raise SchemaOutOfDate(expected=expected, actual=result)
