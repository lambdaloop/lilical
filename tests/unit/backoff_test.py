import random

from lilical.sync.engine import _next_backoff


def test_backoff_starts_at_minimum() -> None:
    delay = _next_backoff(0)
    assert 2.5 <= delay <= 7.5


def test_backoff_doubles() -> None:
    delay = _next_backoff(5)
    assert 5 <= delay <= 15


def test_backoff_is_capped() -> None:
    delay = _next_backoff(300)
    assert delay <= 450


def test_backoff_has_jitter() -> None:
    random.seed(42)
    results = {_next_backoff(10) for _ in range(100)}
    assert len(results) > 1
