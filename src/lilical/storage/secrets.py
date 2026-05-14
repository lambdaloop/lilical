from __future__ import annotations

import contextlib
import json

from lilical.config import Config

_KEYRING_SERVICE = "lilical"


def _build_key(account_id: str) -> str:
    return f"account:{account_id}"


class SecretsStore:
    def __init__(self, data: dict[str, dict[str, str]] | None = None) -> None:
        self._data = data or {}

    @classmethod
    def open(cls, config: Config) -> SecretsStore:
        try:
            import keyring
        except ImportError:
            return cls()
        store = cls()
        try:
            raw = keyring.get_password(_KEYRING_SERVICE, "_index")
            if raw:
                ids = json.loads(raw)
                for account_id in ids:
                    try:
                        entry = keyring.get_password(
                            _KEYRING_SERVICE, _build_key(account_id)
                        )
                        if entry:
                            store._data[account_id] = json.loads(entry)
                    except Exception:
                        pass
        except Exception:
            pass
        return store

    def get(self, account_id: str) -> dict[str, str] | None:
        return self._data.get(account_id)

    def set(self, account_id: str, secrets: dict[str, str]) -> None:
        self._data[account_id] = secrets
        try:
            import keyring

            keyring.set_password(
                _KEYRING_SERVICE,
                _build_key(account_id),
                json.dumps(secrets),
            )
            index = list(self._data.keys())
            keyring.set_password(_KEYRING_SERVICE, "_index", json.dumps(index))
        except Exception:
            pass

    def delete(self, account_id: str) -> None:
        self._data.pop(account_id, None)
        try:
            import keyring

            with contextlib.suppress(Exception):
                keyring.delete_password(_KEYRING_SERVICE, _build_key(account_id))
            index = list(self._data.keys())
            keyring.set_password(_KEYRING_SERVICE, "_index", json.dumps(index))
        except Exception:
            pass
