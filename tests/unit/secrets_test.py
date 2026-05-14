from __future__ import annotations

from unittest.mock import patch

from lilical.config import Config
from lilical.storage.secrets import SecretsStore


def test_secrets_get_set_delete() -> None:
    """Bug 16: SecretsStore basic get/set/delete operations."""
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


def test_secrets_delete_nonexistent_does_not_raise() -> None:
    """Bug 16: Deleting a non-existent key should not raise."""
    store = SecretsStore()
    store.delete("no-such-account")


def test_secrets_update_overwrites() -> None:
    """Bug 16: Updating an existing account overwrites the secret."""
    store = SecretsStore()
    store.set("acc-1", {"token": "old"})
    store.set("acc-1", {"token": "new", "extra": "value"})
    assert store.get("acc-1") == {"token": "new", "extra": "value"}


@patch("keyring.set_password")
@patch("keyring.delete_password")
def test_secrets_set_calls_keyring_per_account(mock_delete, mock_set) -> None:
    """Bug 16: set() writes per-account keyring entries, not a single JSON blob."""
    store = SecretsStore()
    store.set("acc-1", {"token": "xyz"})
    store.set("acc-2", {"token": "abc"})

    mock_set.assert_any_call("lilical", "account:acc-1", '{"token": "xyz"}')
    mock_set.assert_any_call("lilical", "account:acc-2", '{"token": "abc"}')
    mock_set.assert_any_call("lilical", "_index", '["acc-1", "acc-2"]')


@patch("keyring.set_password")
@patch("keyring.delete_password")
def test_secrets_does_not_overwrite_other_accounts_on_delete(
    mock_delete, mock_set
) -> None:
    """Bug 16: Deleting one account does not affect other accounts' data."""
    store = SecretsStore(data={"acc-a": {"token": "a"}, "acc-b": {"token": "b"}})
    store.delete("acc-a")

    assert store.get("acc-b") == {"token": "b"}
    mock_delete.assert_called_once_with("lilical", "account:acc-a")


@patch("keyring.get_password")
def test_secrets_open_reads_per_account(mock_get) -> None:
    """Bug 16: open() reads per-account entries via _index."""
    mock_get.side_effect = lambda service, key: {
        ("lilical", "_index"): '["acc-a", "acc-b"]',
        ("lilical", "account:acc-a"): '{"token": "aaa"}',
        ("lilical", "account:acc-b"): '{"token": "bbb"}',
    }.get((service, key))

    store = SecretsStore.open(Config())
    assert store.get("acc-a") == {"token": "aaa"}
    assert store.get("acc-b") == {"token": "bbb"}
