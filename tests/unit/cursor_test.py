"""Round-trip and discrimination tests for cursor_from_json / cursor_to_json."""

import pytest

from lilical.backends.caldav import CalDavCursor
from lilical.backends.google import GoogleCursor
from lilical.backends.graph import GraphCursor
from lilical.sync.cursor import cursor_from_json, cursor_to_json

# -- round-trips ---------------------------------------------------------------


def test_graph_cursor_roundtrip() -> None:
    c = GraphCursor(delta_link="https://example.com/delta?$token=abc")
    result = cursor_from_json(cursor_to_json(c))
    assert isinstance(result, GraphCursor)
    assert result.delta_link == c.delta_link


def test_google_cursor_roundtrip() -> None:
    c = GoogleCursor(sync_token="goog-tok-xyz")
    result = cursor_from_json(cursor_to_json(c))
    assert isinstance(result, GoogleCursor)
    assert result.sync_token == "goog-tok-xyz"


def test_caldav_cursor_roundtrip() -> None:
    c = CalDavCursor(sync_token="http://dav/sync/1", ctag="etag-42")
    result = cursor_from_json(cursor_to_json(c))
    assert isinstance(result, CalDavCursor)
    assert result.sync_token == "http://dav/sync/1"
    assert result.ctag == "etag-42"


# -- explicit dispatch ---------------------------------------------------------


def test_dispatch_graph() -> None:
    result = cursor_from_json({"_type": "graph", "delta_link": "https://x"})
    assert isinstance(result, GraphCursor)
    assert result.delta_link == "https://x"


def test_dispatch_google() -> None:
    result = cursor_from_json({"_type": "google", "sync_token": "tok"})
    assert isinstance(result, GoogleCursor)
    assert result.sync_token == "tok"


def test_dispatch_caldav() -> None:
    result = cursor_from_json({"_type": "caldav", "sync_token": "s", "ctag": "c"})
    assert isinstance(result, CalDavCursor)
    assert result.sync_token == "s"
    assert result.ctag == "c"


# -- cross-type rejection ------------------------------------------------------


def test_graph_from_json_rejects_wrong_type() -> None:
    with pytest.raises(ValueError):
        GraphCursor.from_json({"_type": "google", "sync_token": "x"})


def test_google_from_json_rejects_wrong_type() -> None:
    with pytest.raises(ValueError):
        GoogleCursor.from_json({"_type": "graph", "delta_link": "x"})


def test_caldav_from_json_rejects_wrong_type() -> None:
    with pytest.raises(ValueError):
        CalDavCursor.from_json({"_type": "graph", "delta_link": "x"})


# -- legacy / unknown cursors --------------------------------------------------


def test_untagged_legacy_cursor_returns_none() -> None:
    # Pre-fix cursors lack _type — treat as "no cursor" → forces initial_sync.
    assert cursor_from_json({"delta_link": "https://x"}) is None
    assert cursor_from_json({"sync_token": "tok"}) is None
    assert cursor_from_json({"sync_token": "s", "ctag": "c"}) is None


def test_none_returns_none() -> None:
    assert cursor_from_json(None) is None


def test_empty_dict_returns_none() -> None:
    assert cursor_from_json({}) is None


def test_unknown_type_returns_none() -> None:
    assert cursor_from_json({"_type": "unknown", "foo": "bar"}) is None
