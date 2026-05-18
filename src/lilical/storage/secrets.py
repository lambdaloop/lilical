from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Protocol

from lilical.config import Config

log = logging.getLogger(__name__)

_KEYRING_SERVICE = "lilical"


def _build_key(account_id: str) -> str:
    return f"account:{account_id}"


def _data_dir(config: Config) -> Path:
    return Path(config.db_path).parent


def _read_machine_id() -> str:
    for candidate in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            value = Path(candidate).read_text().strip()
            if value:
                return value
        except OSError:
            continue
    raise OSError("no machine-id found at /etc/machine-id or /var/lib/dbus/machine-id")


def _derive_key(machine_id: str) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"lilical credentials encryption v1",
    ).derive(machine_id.encode())


# ---------------------------------------------------------------------------
# Backend protocol
# ---------------------------------------------------------------------------


class _Backend(Protocol):
    def read(self, account_id: str) -> str | None: ...
    def write(self, account_id: str, raw: str) -> None: ...
    def remove(self, account_id: str) -> None: ...


class _KeyringBackend:
    def read(self, account_id: str) -> str | None:
        import keyring

        return keyring.get_password(_KEYRING_SERVICE, _build_key(account_id))

    def write(self, account_id: str, raw: str) -> None:
        import keyring

        keyring.set_password(_KEYRING_SERVICE, _build_key(account_id), raw)

    def remove(self, account_id: str) -> None:
        import keyring

        with contextlib.suppress(Exception):
            keyring.delete_password(_KEYRING_SERVICE, _build_key(account_id))


class _FileBackend:
    """AES-GCM encrypted JSON file fallback.

    The encryption key is derived from /etc/machine-id via HKDF-SHA256 — no
    key file is stored alongside the ciphertext, so moving the data dir to
    another machine renders the secrets unreadable there.

    credentials.enc layout: nonce(12 bytes) || AESGCM(key, nonce, plaintext)
    where plaintext is a JSON object mapping account_id → raw_secret_string.
    """

    def __init__(self, data_dir: Path, machine_id: str | None = None) -> None:
        self._enc_file = data_dir / "credentials.enc"
        self._machine_id = machine_id  # override for testing

    def _key(self) -> bytes | None:
        try:
            mid = (
                self._machine_id
                if self._machine_id is not None
                else _read_machine_id()
            )
            return _derive_key(mid)
        except Exception:
            log.error(
                "Cannot derive credentials encryption key: machine-id unavailable. "
                "Secrets will not persist across restarts.",
            )
            return None

    def _load_all(self) -> dict[str, str]:
        if not self._enc_file.exists():
            return {}
        key = self._key()
        if key is None:
            return {}
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            raw = self._enc_file.read_bytes()
            if len(raw) < 12:
                return {}
            nonce, ciphertext = raw[:12], raw[12:]
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
            return json.loads(plaintext)
        except Exception:
            log.exception("credentials.enc: decryption failed; treating as empty")
            return {}

    def _save_all(self, data: dict[str, str]) -> None:
        key = self._key()
        if key is None:
            return
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = os.urandom(12)
        ciphertext = AESGCM(key).encrypt(nonce, json.dumps(data).encode(), None)
        self._enc_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=self._enc_file.parent, prefix=".credentials.enc."
        )
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(nonce + ciphertext)
            os.replace(tmp, self._enc_file)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def read(self, account_id: str) -> str | None:
        return self._load_all().get(account_id)

    def write(self, account_id: str, raw: str) -> None:
        data = self._load_all()
        data[account_id] = raw
        self._save_all(data)

    def remove(self, account_id: str) -> None:
        data = self._load_all()
        if account_id in data:
            data.pop(account_id)
            self._save_all(data)


# ---------------------------------------------------------------------------
# SecretsStore
# ---------------------------------------------------------------------------


class SecretsStore:
    """Per-account secret storage backed by the system keyring or an
    AES-GCM-encrypted file when no keyring is available.

    The DB is the source of truth for which accounts exist; this store just
    holds their passwords/tokens. `get` reads the backend lazily on cache miss,
    so a stale or missing index can't orphan a secret.
    """

    def __init__(
        self,
        data: dict[str, dict[str, str]] | None = None,
        backend: _Backend | None = None,
    ) -> None:
        self._data: dict[str, dict[str, str]] = data or {}
        self._backend: _Backend = backend if backend is not None else _KeyringBackend()

    @classmethod
    def open(cls, config: Config) -> SecretsStore:
        try:
            import keyring
            import keyring.backends.fail

            if isinstance(keyring.get_keyring(), keyring.backends.fail.Keyring):
                enc_path = _data_dir(config) / "credentials.enc"
                log.warning(
                    "No system keyring available (no Secret Service on D-Bus); "
                    "falling back to encrypted file at %s. "
                    "Install gnome-keyring (or another Secret Service provider) "
                    "to use the system keyring instead.",
                    enc_path,
                )
                return cls(backend=_FileBackend(_data_dir(config)))
        except ImportError:
            pass
        return cls()

    def get(self, account_id: str) -> dict[str, str] | None:
        cached = self._data.get(account_id)
        if cached is not None:
            return cached
        try:
            raw = self._backend.read(account_id)
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
            self._backend.write(account_id, json.dumps(secrets))
        except Exception:
            log.exception("keyring write failed for account %s", account_id)

    def delete(self, account_id: str) -> None:
        self._data.pop(account_id, None)
        try:
            self._backend.remove(account_id)
        except Exception:
            log.exception("keyring delete failed for account %s", account_id)
