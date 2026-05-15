"""Tests for the notes-rendering branch in EventDetailsDialog."""

from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication(sys.argv)


def _notes_text(description: str) -> str:
    """Return the string that would be set on the notes QLabel."""
    from lilical.ui.widgets.event_details_dialog import _is_html, _linkify

    return description if _is_html(description) else _linkify(description)


def test_plain_text_url_is_linkified(qapp):
    text = "See https://example.com for details"
    result = _notes_text(text)
    assert '<a href="https://example.com">' in result
    assert "See " in result


def test_plain_text_no_url_is_escaped(qapp):
    text = "Hello & goodbye <world>"
    result = _notes_text(text)
    assert "&amp;" in result
    assert "&lt;" in result
    assert "<world>" not in result


def test_html_description_passes_through_unchanged(qapp):
    html = "<p>Hello <a href='https://example.com'>link</a></p>"
    result = _notes_text(html)
    assert result == html


def test_html_description_not_double_escaped(qapp):
    html = "<b>Bold</b> &amp; <i>italic</i>"
    result = _notes_text(html)
    assert "<b>Bold</b>" in result
    assert "&amp;amp;" not in result
