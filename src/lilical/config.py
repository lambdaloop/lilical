from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_db_path() -> str:
    return str(
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        / "lilical"
        / "lilical.db"
    )


def _default_config_path() -> str:
    return str(
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        / "lilical"
        / "config.toml"
    )


def _default_cache_path() -> str:
    return str(
        Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "lilical"
    )


@dataclass
class Config:
    db_path: str = field(default_factory=_default_db_path)
    config_path: str = field(default_factory=_default_config_path)
    cache_path: str = field(default_factory=_default_cache_path)
    poll_interval: int = 300
    hide_notification_contents: bool = False

    @classmethod
    def load(cls) -> Config:
        return cls()
