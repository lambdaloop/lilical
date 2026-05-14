from __future__ import annotations

from lilical.config import Config


class SecretsStore:
    def __init__(self) -> None:
        pass

    @classmethod
    def open(cls, config: Config) -> SecretsStore:
        return cls()
