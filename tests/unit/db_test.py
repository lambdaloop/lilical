"""Tests for storage.db: PRAGMA verification and ensure_schema."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect

from lilical.storage.db import ensure_schema, open_engine


def test_open_engine_enables_wal(tmp_path: Path) -> None:
    engine = open_engine(str(tmp_path / "test.db"))
    with engine.connect() as conn:
        result = conn.exec_driver_sql("PRAGMA journal_mode").fetchone()
    assert result[0] == "wal"


def test_open_engine_enables_foreign_keys(tmp_path: Path) -> None:
    engine = open_engine(str(tmp_path / "test.db"))
    with engine.connect() as conn:
        result = conn.exec_driver_sql("PRAGMA foreign_keys").fetchone()
    assert result[0] == 1


def test_open_engine_sets_synchronous_normal(tmp_path: Path) -> None:
    engine = open_engine(str(tmp_path / "test.db"))
    with engine.connect() as conn:
        result = conn.exec_driver_sql("PRAGMA synchronous").fetchone()
    # 1 = NORMAL
    assert result[0] == 1


def test_open_engine_sets_busy_timeout(tmp_path: Path) -> None:
    engine = open_engine(str(tmp_path / "test.db"))
    with engine.connect() as conn:
        result = conn.exec_driver_sql("PRAGMA busy_timeout").fetchone()
    assert result[0] == 5000


def test_ensure_schema_creates_required_tables(tmp_path: Path) -> None:
    engine = open_engine(str(tmp_path / "migrate.db"))
    ensure_schema(engine)
    tables = inspect(engine).get_table_names()
    for expected in (
        "accounts",
        "calendars",
        "events",
        "event_instances",
        "pending_ops",
        "settings",
    ):
        assert expected in tables, f"Table {expected!r} missing after ensure_schema"


def test_ensure_schema_is_idempotent(tmp_path: Path) -> None:
    engine = open_engine(str(tmp_path / "idem.db"))
    ensure_schema(engine)
    # Running a second time must not raise.
    ensure_schema(engine)
    tables = inspect(engine).get_table_names()
    assert "events" in tables
