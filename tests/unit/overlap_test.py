"""Tests for the pure overlap-layout helper ui/views/_overlap.pack_overlapping."""

from __future__ import annotations

from lilical.ui.views._overlap import pack_overlapping


def test_empty_input_returns_empty() -> None:
    assert pack_overlapping([]) == []


def test_single_item_gets_col_0_cols_1_xspan_1() -> None:
    result = pack_overlapping([(0, 60, "a")])
    assert result == [(0, 1, 1, "a")]


def test_disjoint_intervals_each_get_col_0() -> None:
    items = [(0, 60, "a"), (120, 180, "b"), (240, 300, "c")]
    result = pack_overlapping(items)
    for col, cols, xspan, _ in result:
        assert col == 0
        assert cols == 1
        assert xspan == 1


def test_two_overlapping_get_columns_0_and_1() -> None:
    items = [(0, 60, "a"), (30, 90, "b")]
    result = pack_overlapping(items)
    cols_used = {col for col, _, _, _ in result}
    assert cols_used == {0, 1}
    # Each has cluster size = 2
    for _col, cols, _xspan, _ in result:
        assert cols == 2


def test_three_way_overlap_each_gets_third_width() -> None:
    items = [(0, 60, "a"), (0, 60, "b"), (0, 60, "c")]
    result = pack_overlapping(items)
    cols_used = {col for col, _, _, _ in result}
    assert cols_used == {0, 1, 2}
    for _col, cols, _, _ in result:
        assert cols == 3


def test_back_to_back_no_overlap() -> None:
    # end == start is NOT considered overlapping per the algorithm.
    items = [(0, 60, "a"), (60, 120, "b")]
    result = pack_overlapping(items)
    for col, cols, xspan, _ in result:
        assert col == 0
        assert cols == 1
        assert xspan == 1


def test_late_starter_reuses_freed_column() -> None:
    # a (0-60) occupies col 0; b (0-60) occupies col 1.
    # c (90-150) starts after both end — it should reuse col 0 in its own cluster.
    items = [(0, 60, "a"), (0, 60, "b"), (90, 150, "c")]
    result = pack_overlapping(items)
    by_payload = {payload: (col, cols, xspan) for col, cols, xspan, payload in result}
    assert by_payload["c"] == (0, 1, 1)


def test_longer_event_gets_leftmost_column() -> None:
    # Two events start at 0, but "long" extends further.
    # The longer one (same start, larger end) should get col 0.
    items = [(0, 120, "long"), (0, 60, "short")]
    result = pack_overlapping(items)
    by_payload = {p: col for col, _, _, p in result}
    assert by_payload["long"] == 0
    assert by_payload["short"] == 1


def test_xspan_stretches_into_free_columns() -> None:
    # a occupies cols 0-2; b occupies only col 1 (overlaps a);
    # c occupies col 2 and overlaps b but not a in col 0.
    # a: starts at 0, ends at 90
    # b: starts at 30, ends at 45 (overlaps a → goes to col 1)
    # a's xspan should be 1 if col 1 is occupied during a's time.
    items = [(0, 90, "a"), (30, 45, "b")]
    result = pack_overlapping(items)
    by_payload = {p: (col, cols, xspan) for col, cols, xspan, p in result}
    # b overlaps a → both in a 2-column cluster
    assert by_payload["a"][0] == 0  # a is in col 0
    assert by_payload["b"][0] == 1  # b is in col 1
    # a's xspan: col 1 is occupied by b during a's time → xspan == 1
    assert by_payload["a"][2] == 1
