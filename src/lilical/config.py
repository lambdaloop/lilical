from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    db_path: str = field(default_factory=lambda: str(
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        / "lilical" / "lilical.db"
    ))
    config_path: str = field(default_factory=lambda: str(
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        / "lilical" / "config.toml"
    ))
    cache_path: str = field(default_factory=lambda: str(
        Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        / "lilical"
    ))
    poll_interval: int = 300
    hide_notification_contents: bool = False

    @classmethod
    def load(cls) -> Config:
        return cls()
