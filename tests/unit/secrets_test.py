from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lilical.config import Config
from lilical.storage.secrets import SecretsStore, _FileBackend

# All tests patch keyring.* — otherwise the in-memory tests below would clobber
# the developer's real system keyring (and have, historically: a stale `_index`
# pointing at test fixtures orphaned real account secrets and caused a forced
# re-auth on every app restart).

_TEST_MACHINE_ID = "aabbccddeeff00112233445566778899"


@patch("keyring.set_password")
@patch("keyring.get_password", return_value=None)
@patch("keyring.delete_password")
def test_secrets_get_set_delete(mock_delete, mock_get, mock_set) -> None:
    """Basic get/set/delete operations round-trip via the in-memory cache."""
    store = SecretsStore()
    assert store.get("missing") is None

    store.set("acc-1", {"token": "abc"})
    assert store.get("acc-1") == {"token": "abc"}

    store.set("acc-2", {"token": "def"})
    assert store.get("acc-1") == {"token": "abc"}
    assert store.get("acc-2") == {"token": "def"}

    store.delete("acc-1")
    assert store.get("acc-1") is None
    assert store.get("acc-2") == {"token": "def"}


@patch("keyring.set_password")
@patch("keyring.get_password", return_value=None)
@patch("keyring.delete_password")
def test_secrets_delete_nonexistent_does_not_raise(
    mock_delete, mock_get, mock_set
) -> None:
    store = SecretsStore()
    store.delete("no-such-account")


@patch("keyring.set_password")
@patch("keyring.get_password", return_value=None)
@patch("keyring.delete_password")
def test_secrets_update_overwrites(mock_delete, mock_get, mock_set) -> None:
    store = SecretsStore()
    store.set("acc-1", {"token": "old"})
    store.set("acc-1", {"token": "new", "extra": "value"})
    assert store.get("acc-1") == {"token": "new", "extra": "value"}


@patch("keyring.set_password")
@patch("keyring.delete_password")
def test_secrets_set_writes_per_account_keyring_entry(mock_delete, mock_set) -> None:
    """set() writes a per-account keyring entry. There is no _index key:
    the DB is the source of truth for which accounts exist, so any separately
    maintained index would only add a way to lose secrets."""
    store = SecretsStore()
    store.set("acc-1", {"token": "xyz"})
    store.set("acc-2", {"token": "abc"})

    mock_set.assert_any_call("lilical", "account:acc-1", '{"token": "xyz"}')
    mock_set.assert_any_call("lilical", "account:acc-2", '{"token": "abc"}')
    # No _index write — that key was the bug that orphaned real secrets.
    written_keys = {call.args[1] for call in mock_set.call_args_list}
    assert "_index" not in written_keys


@patch("keyring.set_password")
@patch("keyring.delete_password")
def test_secrets_does_not_overwrite_other_accounts_on_delete(
    mock_delete, mock_set
) -> None:
    store = SecretsStore(data={"acc-a": {"token": "a"}, "acc-b": {"token": "b"}})
    store.delete("acc-a")

    assert store.get("acc-b") == {"token": "b"}
    mock_delete.assert_called_once_with("lilical", "account:acc-a")


@patch("keyring.get_password")
def test_secrets_get_reads_from_keyring_on_cache_miss(mock_get) -> None:
    """The fix for the re-auth-on-restart bug: even if open() did not
    pre-load an account, get() must still find its secret in keyring."""
    mock_get.side_effect = lambda service, key: {
        ("lilical", "account:acc-a"): '{"token": "aaa"}',
    }.get((service, key))

    store = SecretsStore.open(Config())
    assert store.get("acc-a") == {"token": "aaa"}
    # Subsequent lookups hit the cache.
    assert store.get("acc-a") == {"token": "aaa"}
    assert mock_get.call_count == 1


@patch("keyring.get_password", return_value=None)
def test_secrets_get_returns_none_when_not_in_keyring(mock_get) -> None:
    store = SecretsStore.open(Config())
    assert store.get("ghost") is None


@patch("keyring.get_password", side_effect=RuntimeError("dbus disconnected"))
def test_secrets_get_returns_none_when_keyring_raises(mock_get) -> None:
    """Keyring backend errors must not crash callers — they get None and
    can prompt the user to re-enter credentials."""
    store = SecretsStore.open(Config())
    assert store.get("acc-a") is None


@patch("keyring.get_password", return_value="this is not json")
def test_secrets_get_returns_none_on_malformed_payload(mock_get) -> None:
    store = SecretsStore.open(Config())
    assert store.get("acc-a") is None


@patch.dict("sys.modules", {"keyring": None})
def test_secrets_get_returns_none_when_keyring_not_installed() -> None:
    """When keyring cannot be imported, get() returns None."""
    store = SecretsStore()
    assert store.get("acc-1") is None


@patch("keyring.set_password", side_effect=RuntimeError("keyring broken"))
@patch("keyring.get_password", return_value=None)
@patch("keyring.delete_password")
def test_secrets_set_handles_keyring_write_failure(
    mock_delete, mock_get, mock_set
) -> None:
    """When keyring.set_password raises, set() does not crash."""
    store = SecretsStore()
    store.set("acc-1", {"token": "abc"})
    assert store.get("acc-1") == {"token": "abc"}


@patch("keyring.set_password")
@patch("keyring.get_password", return_value=None)
@patch("keyring.delete_password", side_effect=RuntimeError("keyring broken"))
def test_secrets_delete_handles_keyring_delete_failure(
    mock_delete, mock_get, mock_set
) -> None:
    """When keyring.delete_password raises, delete() does not crash."""
    store = SecretsStore(data={"acc-1": {"token": "abc"}})
    store.delete("acc-1")
    assert store.get("acc-1") is None


@patch.dict("sys.modules", {"keyring": None})
def test_secrets_delete_handles_keyring_import_error() -> None:
    """When keyring cannot be imported, delete() does not crash."""
    store = SecretsStore(data={"acc-1": {"token": "abc"}})
    store.delete("acc-1")
    assert store.get("acc-1") is None


# ---------------------------------------------------------------------------
# _FileBackend unit tests
# All tests pass machine_id explicitly so they don't depend on /etc/machine-id.
# ---------------------------------------------------------------------------


def test_file_backend_round_trip(tmp_path: Path) -> None:
    fb = _FileBackend(tmp_path, machine_id=_TEST_MACHINE_ID)
    assert fb.read("x") is None
    fb.write("acc-1", '{"token":"a"}')
    fb.write("acc-2", '{"token":"b"}')
    assert fb.read("acc-1") == '{"token":"a"}'
    assert fb.read("acc-2") == '{"token":"b"}'
    fb.remove("acc-1")
    assert fb.read("acc-1") is None
    assert fb.read("acc-2") == '{"token":"b"}'


def test_file_backend_persists_across_instances(tmp_path: Path) -> None:
    fb = _FileBackend(tmp_path, machine_id=_TEST_MACHINE_ID)
    fb.write("acc-1", '{"token":"persisted"}')
    result = _FileBackend(tmp_path, machine_id=_TEST_MACHINE_ID).read("acc-1")
    assert result == '{"token":"persisted"}'


def test_file_backend_different_machine_id_cannot_decrypt(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Secrets written on machine A must not be readable on machine B."""
    _FileBackend(tmp_path, machine_id="machine-id-A").write("acc-1", '{"token":"s"}')
    with caplog.at_level("ERROR"):
        result = _FileBackend(tmp_path, machine_id="machine-id-B").read("acc-1")
    assert result is None
    assert "decryption failed" in caplog.text


def test_file_backend_get_missing_returns_none_without_error(tmp_path: Path) -> None:
    assert _FileBackend(tmp_path, machine_id=_TEST_MACHINE_ID).read("ghost") is None


def test_file_backend_remove_missing_account_is_noop(tmp_path: Path) -> None:
    fb = _FileBackend(tmp_path, machine_id=_TEST_MACHINE_ID)
    fb.write("acc-1", '{"token":"a"}')
    fb.remove("ghost")
    assert fb.read("acc-1") == '{"token":"a"}'


def test_file_backend_corrupt_ciphertext_returns_none_and_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fb = _FileBackend(tmp_path, machine_id=_TEST_MACHINE_ID)
    fb.write("acc-1", '{"token":"a"}')
    (tmp_path / "credentials.enc").write_bytes(b"\x00" * 64)
    with caplog.at_level("ERROR"):
        result = fb.read("acc-1")
    assert result is None
    assert "decryption failed" in caplog.text


def test_file_backend_missing_machine_id_degrades_gracefully(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """If machine-id can't be read, write() is a no-op and read() returns None."""
    err = OSError("no machine-id")
    with patch("lilical.storage.secrets._read_machine_id", side_effect=err), \
            caplog.at_level("ERROR"):
        fb = _FileBackend(tmp_path)
        fb.write("acc-1", '{"token":"x"}')
        assert not (tmp_path / "credentials.enc").exists()
        assert fb.read("acc-1") is None
    assert "machine-id" in caplog.text.lower()


# ---------------------------------------------------------------------------
# SecretsStore.open() backend selection
# ---------------------------------------------------------------------------


def test_secrets_store_open_uses_file_backend_when_no_keyring(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import keyring.backends.fail

    config = Config(db_path=str(tmp_path / "lilical.db"))
    fail_kr = keyring.backends.fail.Keyring()
    mid_patch = patch(
        "lilical.storage.secrets._read_machine_id", return_value=_TEST_MACHINE_ID
    )
    kr_patch = patch("keyring.get_keyring", return_value=fail_kr)
    with kr_patch, mid_patch, caplog.at_level("WARNING"):
        store = SecretsStore.open(config)
        store.set("acc-1", {"token": "file-persisted"})

    assert "falling back to encrypted file" in caplog.text.lower()
    assert (tmp_path / "credentials.enc").exists()


def test_secrets_store_open_emits_exactly_one_warning_on_fallback(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import keyring.backends.fail

    config = Config(db_path=str(tmp_path / "lilical.db"))
    with patch("keyring.get_keyring", return_value=keyring.backends.fail.Keyring()), \
            caplog.at_level("WARNING"):
        SecretsStore.open(config)

    warnings = [
        r for r in caplog.records
        if r.levelname == "WARNING" and "falling back" in r.message.lower()
    ]
    assert len(warnings) == 1


def test_secrets_store_open_uses_keyring_backend_when_available() -> None:
    mock_kr = MagicMock()

    with patch("keyring.get_keyring", return_value=mock_kr), \
            patch("keyring.set_password") as mock_set:
        store = SecretsStore.open(Config())
        store.set("acc-1", {"token": "via-keyring"})
        mock_set.assert_called_once()
