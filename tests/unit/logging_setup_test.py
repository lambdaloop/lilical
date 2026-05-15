"""Tests for lilical.logging_setup.setup_logging."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from lilical.logging_setup import setup_logging


def test_setup_logging_calls_basicconfig_with_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LILICAL_LOG_LEVEL", raising=False)
    with patch("lilical.logging_setup.logging.basicConfig") as mock_cfg:
        setup_logging()
    assert mock_cfg.called
    handlers = mock_cfg.call_args.kwargs.get("handlers", [])
    import sys

    assert any(getattr(h, "stream", None) is sys.stderr for h in handlers)


def test_default_level_is_info(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LILICAL_LOG_LEVEL", raising=False)
    with patch("lilical.logging_setup.logging.basicConfig") as mock_cfg:
        setup_logging()
    assert mock_cfg.call_args.kwargs["level"] == "INFO"


def test_lilical_log_level_env_sets_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LILICAL_LOG_LEVEL", "DEBUG")
    with patch("lilical.logging_setup.logging.basicConfig") as mock_cfg:
        setup_logging()
    assert mock_cfg.call_args.kwargs["level"] == "DEBUG"


def test_debug_mode_raises_http_library_loggers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LILICAL_LOG_LEVEL", "DEBUG")
    for name in ("caldav", "urllib3.connectionpool", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.NOTSET)

    with patch("lilical.logging_setup.logging.basicConfig"):
        setup_logging()

    for name in ("caldav", "urllib3.connectionpool", "httpx", "httpcore"):
        assert logging.getLogger(name).level == logging.DEBUG, name


def test_non_debug_does_not_raise_library_loggers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LILICAL_LOG_LEVEL", "INFO")
    for name in ("caldav", "httpx"):
        logging.getLogger(name).setLevel(logging.NOTSET)

    with patch("lilical.logging_setup.logging.basicConfig"):
        setup_logging()

    for name in ("caldav", "httpx"):
        assert logging.getLogger(name).level != logging.DEBUG, name


def test_journald_handler_added_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """If systemd.journal is importable, a JournalHandler should be passed to basicConfig."""
    import types

    class _FakeJournalHandler(logging.Handler):
        def __init__(self, SYSLOG_IDENTIFIER=""):  # noqa: N803
            super().__init__()

        def emit(self, record) -> None:  # type: ignore[override]
            pass

    fake_journal = types.ModuleType("systemd.journal")
    fake_journal.JournalHandler = _FakeJournalHandler  # type: ignore[attr-defined]
    fake_systemd = types.ModuleType("systemd")
    fake_systemd.journal = fake_journal  # type: ignore[attr-defined]

    monkeypatch.setitem(__import__("sys").modules, "systemd", fake_systemd)
    monkeypatch.setitem(__import__("sys").modules, "systemd.journal", fake_journal)
    monkeypatch.delenv("LILICAL_LOG_LEVEL", raising=False)

    with patch("lilical.logging_setup.logging.basicConfig") as mock_cfg:
        setup_logging()

    handlers = mock_cfg.call_args.kwargs.get("handlers", [])
    assert any(isinstance(h, _FakeJournalHandler) for h in handlers)
