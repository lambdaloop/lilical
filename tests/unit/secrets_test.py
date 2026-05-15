from __future__ import annotations

from unittest.mock import patch

from lilical.config import Config
from lilical.storage.secrets import SecretsStore

# All tests patch keyring.* — otherwise the in-memory tests below would clobber
# the developer's real system keyring (and have, historically: a stale `_index`
# pointing at test fixtures orphaned real account secrets and caused a forced
# re-auth on every app restart).


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
