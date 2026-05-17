"""Tests for backends.factory.build_backend_factory kind dispatch."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from lilical.backends.factory import build_backend_factory


def _account(kind: str, **extra) -> SimpleNamespace:
    return SimpleNamespace(
        id="acc-test",
        kind=kind,
        identity="user@example.com",
        server_url=extra.get("server_url"),
        include_contacts=extra.get("include_contacts", 0),
    )


def _secrets(data: dict | None = None) -> MagicMock:
    mock = MagicMock()
    mock.get.return_value = data or {}
    return mock


def test_caldav_kind_returns_caldav_backend() -> None:
    from lilical.backends.caldav import CalDavBackend

    factory = build_backend_factory(_secrets())
    backend = factory(_account("caldav", server_url="https://dav.example.com"))
    assert isinstance(backend, CalDavBackend)


def test_google_kind_returns_google_backend() -> None:
    from lilical.backends.google import GoogleBackend

    factory = build_backend_factory(_secrets({"token": '{"token_type":"Bearer"}'}))
    backend = factory(_account("google"))
    assert isinstance(backend, GoogleBackend)


def test_graph_kind_returns_graph_backend() -> None:
    from lilical.backends.graph import GraphBackend

    factory = build_backend_factory(_secrets({"msal_cache": "{}"}))
    backend = factory(_account("graph"))
    assert isinstance(backend, GraphBackend)


def test_unknown_kind_raises_not_implemented() -> None:
    factory = build_backend_factory(_secrets())
    with pytest.raises(NotImplementedError):
        factory(_account("outlook365"))


def test_caldav_backend_receives_server_url() -> None:
    from lilical.backends.caldav import CalDavBackend

    factory = build_backend_factory(_secrets({"password": "s3cr3t"}))
    backend = factory(_account("caldav", server_url="https://radicale.example.com"))
    assert isinstance(backend, CalDavBackend)
    assert backend._server_url == "https://radicale.example.com"


def test_google_backend_receives_token_json() -> None:
    from lilical.backends.google import GoogleBackend

    token = '{"token_type":"Bearer","refresh_token":"rt-abc"}'
    factory = build_backend_factory(_secrets({"token": token}))
    backend = factory(_account("google"))
    assert isinstance(backend, GoogleBackend)
    assert backend._token_json == token


def test_graph_backend_receives_msal_cache() -> None:
    from lilical.backends.graph import GraphBackend

    cache = '{"AccessToken":{}}'
    factory = build_backend_factory(_secrets({"msal_cache": cache}))
    backend = factory(_account("graph"))
    assert isinstance(backend, GraphBackend)
    assert backend._cache_json == cache


def test_factory_sets_refresh_callback_for_google() -> None:
    from lilical.backends.google import GoogleBackend

    secrets = _secrets({"token": "{}"})
    factory = build_backend_factory(secrets)
    backend = factory(_account("google"))
    assert isinstance(backend, GoogleBackend)
    assert callable(backend._on_token_refreshed)


def test_factory_sets_refresh_callback_for_graph() -> None:
    from lilical.backends.graph import GraphBackend

    secrets = _secrets({"msal_cache": "{}"})
    factory = build_backend_factory(secrets)
    backend = factory(_account("graph"))
    assert isinstance(backend, GraphBackend)
    assert callable(backend._on_token_refreshed)


def test_save_google_token_persists_to_secrets() -> None:
    """The _save_google_token closure writes to the secrets store."""
    secrets = MagicMock()
    secrets.get.return_value = {}
    factory = build_backend_factory(secrets)
    backend = factory(_account("google"))
    from lilical.backends.google import GoogleBackend

    assert isinstance(backend, GoogleBackend)
    backend._on_token_refreshed('{"access_token": "new-tok"}')
    secrets.set.assert_called_once_with(
        "acc-test", {"token": '{"access_token": "new-tok"}'}
    )


def test_save_graph_cache_persists_to_secrets() -> None:
    """The _save_graph_cache closure merges into existing secrets."""
    secrets = MagicMock()
    secrets.get.return_value = {"msal_cache": "{}"}
    factory = build_backend_factory(secrets)
    backend = factory(_account("graph"))
    from lilical.backends.graph import GraphBackend

    assert isinstance(backend, GraphBackend)
    backend._on_token_refreshed('{"AccessToken": {}}')
    expected_cache = secrets.set.call_args[0][1]["msal_cache"]
    assert expected_cache == '{"AccessToken": {}}'


def test_save_graph_cache_creates_secrets_when_missing() -> None:
    """When no secrets exist yet, _save_graph_cache creates the dict."""
    secrets = MagicMock()
    secrets.get.return_value = None
    factory = build_backend_factory(secrets)
    backend = factory(_account("graph"))
    from lilical.backends.graph import GraphBackend

    assert isinstance(backend, GraphBackend)
    backend._on_token_refreshed('{"AccessToken": {}}')
    secrets.set.assert_called_once()
    saved = secrets.set.call_args[0][1]
    assert saved["msal_cache"] == '{"AccessToken": {}}'
