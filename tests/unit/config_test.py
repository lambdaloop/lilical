from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from lilical.config import Config, _default_db_path


def test_default_db_path_uses_xdg_data_home() -> None:
    """Bug 17: _default_db_path uses XDG_DATA_HOME when set."""
    with patch.dict(os.environ, {"XDG_DATA_HOME": "/custom/data"}):
        path = _default_db_path()
    assert path.startswith("/custom/data/lilical/lilical.db")


def test_default_db_path_falls_back_to_home() -> None:
    """Bug 17: _default_db_path falls back to ~/.local/share."""
    with (
        patch.dict(os.environ, {}, clear=True),
        patch("pathlib.Path.home", return_value=Path("/home/testuser")),
    ):
        path = _default_db_path()
    assert path.startswith("/home/testuser/.local/share/lilical/lilical.db")


def test_default_db_path_never_uses_cwd() -> None:
    """Bug 17: _default_db_path should never use cwd (removed cwd fallback)."""
    with (
        patch.dict(os.environ, {}, clear=True),
        patch("pathlib.Path.home", return_value=Path("/home/testuser")),
    ):
        path = _default_db_path()
    assert "cwd" not in path
    assert os.getcwd() not in path


def test_config_load_returns_defaults() -> None:
    """Config.load returns a Config with defaults."""
    c = Config.load()
    assert isinstance(c.poll_interval, int)
    assert c.poll_interval == 300
    assert c.hide_notification_contents is False
    assert c.db_path
    assert c.config_path
    assert c.cache_path
