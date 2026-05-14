from __future__ import annotations

import contextlib
import json
import logging

from lilical.config import Config

log = logging.getLogger(__name__)

_KEYRING_SERVICE = "lilical"


def _build_key(account_id: str) -> str:
    return f"account:{account_id}"


class SecretsStore:
    """Per-account secret storage backed by the system keyring.

    The DB is the source of truth for which accounts exist; this store just
    holds their passwords/tokens. `get` reads keyring lazily on cache miss, so
    a stale or missing index can't orphan a secret (previously, `open()`
    pre-loaded entries from a separately-maintained `_index` key — if that
    index got clobbered, real secrets would still be in keyring but never
    discovered, manifesting as forced re-auth on every restart).
    """

    def __init__(self, data: dict[str, dict[str, str]] | None = None) -> None:
        self._data: dict[str, dict[str, str]] = data or {}

    @classmethod
    def open(cls, config: Config) -> SecretsStore:
        # No eager load: get() reads from keyring on demand. This keeps a
        # corrupted or test-clobbered keyring index from silently dropping
        # real secrets.
        return cls()

    def get(self, account_id: str) -> dict[str, str] | None:
        cached = self._data.get(account_id)
        if cached is not None:
            return cached
        try:
            import keyring
        except ImportError:
            return None
        try:
            raw = keyring.get_password(_KEYRING_SERVICE, _build_key(account_id))
        except Exception:
            log.exception("keyring read failed for account %s", account_id)
            return None
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except Exception:
            log.exception("malformed keyring payload for account %s", account_id)
            return None
        self._data[account_id] = value
        return value

    def set(self, account_id: str, secrets: dict[str, str]) -> None:
        self._data[account_id] = secrets
        try:
            import keyring

            keyring.set_password(
                _KEYRING_SERVICE,
                _build_key(account_id),
                json.dumps(secrets),
            )
        except Exception:
            log.exception("keyring write failed for account %s", account_id)

    def delete(self, account_id: str) -> None:
        self._data.pop(account_id, None)
        try:
            import keyring

            with contextlib.suppress(Exception):
                keyring.delete_password(_KEYRING_SERVICE, _build_key(account_id))
        except Exception:
            log.exception("keyring delete failed for account %s", account_id)
