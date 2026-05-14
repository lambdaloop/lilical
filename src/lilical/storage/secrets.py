from __future__ import annotations

import json

from lilical.config import Config


class SecretsStore:
    def __init__(self, data: dict[str, dict[str, str]] | None = None) -> None:
        self._data = data or {}

    @classmethod
    def open(cls, config: Config) -> SecretsStore:
        try:
            import keyring
        except ImportError:
            return cls()
        try:
            raw = keyring.get_password("lilical", "credentials")
            if raw:
                return cls(data=json.loads(raw))
        except Exception:
            pass
        return cls()

    def get(self, account_id: str) -> dict[str, str] | None:
        return self._data.get(account_id)

    def set(self, account_id: str, secrets: dict[str, str]) -> None:
        self._data[account_id] = secrets
        try:
            import keyring
            keyring.set_password("lilical", "credentials", json.dumps(self._data))
        except ImportError:
            pass

    def delete(self, account_id: str) -> None:
        self._data.pop(account_id, None)
        try:
            import keyring
            keyring.set_password("lilical", "credentials", json.dumps(self._data))
        except ImportError:
            pass
