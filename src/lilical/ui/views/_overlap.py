"""Cascade overlap-layout helper for Week + Day views.

The algorithm — Google Calendar / Business Calendar 2 style:

1. Sort items by start time, longest-first as the tiebreak so that an
   enclosing event gets the leftmost column.
2. Walk the sorted list, grouping transitively-overlapping items into
   clusters. Within each cluster, place every item in the lowest-indexed
   column where it doesn't conflict in time with the column's last entry.
3. For every item, compute its `xspan` — how many columns to the right it
   can stretch before bumping into another item in this cluster. This is
   what produces the "cascade" effect: a 30-minute lunch tucked inside a
   3-hour meeting expands across whatever inner columns remain free.

`pack_overlapping([...])` returns parallel-indexed tuples of
`(col, cols, xspan, payload)` for each input item.

`pack_overlapping_lanes([...], col_w)` is a higher-level wrapper used by
Week and Day views. It falls back to the cascade for sparse clusters but
switches dense clusters (where per-chip width would drop below
MIN_NORMAL_CHIP_W) to a dominant-chip + vertical-line-spine layout where
one event is shown as a normal chip and all others are thin colored bars
in a right-side gutter.
"""

from __future__ import annotations

from typing import Any, Callable

# Tunables for pack_overlapping_lanes.
MIN_NORMAL_CHIP_W: float = 60.0   # column width below which cluster mode triggers


def pack_overlapping(
    items: list[tuple[float, float, Any]],
) -> list[tuple[int, int, int, Any]]:
    """Layout overlapping items into columns with cascade widths.

    Args:
        items: list of ``(start, end, payload)`` tuples; start/end are any
            comparable values (e.g. minutes from midnight).

    Returns:
        list of ``(col, cols, xspan, payload)``, parallel-indexed to *items*.
        - ``col`` is the assigned column within the cluster (0-based).
        - ``cols`` is the number of columns used by this item's cluster.
        - ``xspan`` is how many columns this item stretches across
          (1 ≤ xspan ≤ cols - col).
    """
    n = len(items)
    if n == 0:
        return []

    # Sort by (start asc, end desc) so longer items at the same start time
    # are placed first into the leftmost column.
    order = sorted(range(n), key=lambda i: (items[i][0], -items[i][1]))

    col_of: list[int] = [0] * n
    cluster_id_of: list[int] = [0] * n
    cluster_size: dict[int, int] = {}  # cluster_id → columns used
    cluster_members: dict[int, list[int]] = {}  # cluster_id → item indices

    cur_id = -1
    cur_end: float | None = None
    cur_cols: list[list[int]] = []  # per-column list of item indices

    def commit() -> None:
        if cur_id < 0:
            return
        cluster_size[cur_id] = len(cur_cols)
        cluster_members[cur_id] = [i for col in cur_cols for i in col]

    for idx in order:
        s, e, _ = items[idx]
        if cur_end is None or s >= cur_end:
            # New cluster — flush the previous one and start fresh.
            commit()
            cur_id += 1
            cur_end = e
            cur_cols = [[idx]]
            col_of[idx] = 0
        else:
            placed: int | None = None
            for col_i, col in enumerate(cur_cols):
                last = col[-1]
                if items[last][1] <= s:
                    col.append(idx)
                    placed = col_i
                    break
            if placed is None:
                cur_cols.append([idx])
                placed = len(cur_cols) - 1
            col_of[idx] = placed
            cur_end = max(cur_end, e)
        cluster_id_of[idx] = cur_id
    commit()

    result: list[tuple[int, int, int, Any]] = []
    for idx in range(n):
        s, e, payload = items[idx]
        col = col_of[idx]
        cid = cluster_id_of[idx]
        cols = cluster_size[cid]
        xspan = 1
        for next_col in range(col + 1, cols):
            conflict = False
            for other in cluster_members[cid]:
                if other == idx or col_of[other] != next_col:
                    continue
                s2, e2, _ = items[other]
                if s2 < e and e2 > s:
                    conflict = True
                    break
            if conflict:
                break
            xspan += 1
        result.append((col, cols, xspan, payload))
    return result


def pick_dominant_event(
    items: list[tuple[float, float, str, Any]],
    is_own_calendar_fn: Callable[[str], bool] | None = None,
) -> int:
    """Return index of dominant event in a cluster.

    Priority: own calendar > longest duration > earliest start.
    ``is_own_calendar_fn(calendar_id) -> bool`` is injected; pass None to
    skip the own-calendar tier and fall back directly to duration.

    Args:
        items: list of ``(start, end, calendar_id, payload)`` tuples.
        is_own_calendar_fn: predicate that returns True for owned calendars.

    Returns:
        Index into *items* of the dominant event.
    """
    if not items:
        return 0

    def key(i: int) -> tuple[int, float, float]:
        start, end, cal_id, _ = items[i]
        is_own = (
            1 if (is_own_calendar_fn is not None and is_own_calendar_fn(cal_id)) else 0
        )
        duration = end - start
        return (-is_own, -duration, start)

    return min(range(len(items)), key=key)


def pack_overlapping_lanes(
    items: list[tuple[float, float, str, Any]],
    col_w: float,
    *,
    min_normal_chip_w: float = MIN_NORMAL_CHIP_W,
    is_own_calendar_fn: Callable[[str], bool] | None = None,
) -> list[tuple[float, float, str, Any]]:
    """Pack items; emit one cluster entry for dense overlap groups.

    For clusters where the cascade would produce columns narrower than
    *min_normal_chip_w*, the whole cluster is collapsed into a single
    ``"cluster"`` entry containing all events and a designated dominant
    event index. Sparse clusters fall back to the standard cascade
    (one ``"normal"`` entry per event).

    Args:
        items: list of ``(start, end, calendar_id, payload)`` tuples.
        col_w: width of the day column in pixels (1px side-padding each side).
        min_normal_chip_w: per-chip width below which cluster mode activates.
        is_own_calendar_fn: predicate for determining dominant event priority.

    Returns:
        List of ``(x_offset, width, mode, data)`` where:
        - ``mode == "normal"``: *data* is the original event payload;
          ``x_offset`` and ``width`` describe a single chip's geometry.
        - ``mode == "cluster"``: *data* is a dict with keys ``events``
          (list of ``{"start_min", "end_min", "cal_id", "payload"}`` dicts),
          ``dominant_index`` (int), ``cluster_start_min`` (float),
          ``cluster_end_min`` (float).  ``x_offset`` and ``width`` describe
          the cluster's geometry spanning the full inner column width.

        The returned list may be shorter than *items* when dense clusters
        collapse multiple items into a single entry.
    """
    n = len(items)
    if n == 0:
        return []

    inner_w = max(1.0, col_w - 2)  # 1px padding on each side

    # Run cascade (drops calendar_id; payload passes through unchanged).
    cascade_items = [(s, e, payload) for s, e, _cid, payload in items]
    cascade = pack_overlapping(cascade_items)

    # Replicate cascade's cluster detection to know which items share a cluster.
    order = sorted(range(n), key=lambda i: (items[i][0], -items[i][1]))
    clusters: list[list[int]] = []
    cur_end: float = -1.0
    for idx in order:
        s, e, _, _ = items[idx]
        if s >= cur_end:
            clusters.append([idx])
            cur_end = e
        else:
            clusters[-1].append(idx)
            cur_end = max(cur_end, e)

    result: list[tuple[float, float, str, Any]] = []

    for members in clusters:
        cols = cascade[members[0]][1]
        per_chip_w = inner_w / max(cols, 1)

        if per_chip_w >= min_normal_chip_w:
            # Sparse: standard cascade geometry.
            sub_w = inner_w / cols
            for idx in members:
                col_i, _, xspan, payload = cascade[idx]
                x_off = 1.0 + col_i * sub_w
                w = max(8.0, xspan * sub_w)
                result.append((x_off, w, "normal", payload))
        else:
            # Dense: collapse into one cluster entry.
            cluster_items = [items[idx] for idx in members]
            dominant_local = pick_dominant_event(cluster_items, is_own_calendar_fn)

            cluster_start_min = min(items[idx][0] for idx in members)
            cluster_end_min = max(items[idx][1] for idx in members)

            events_data = [
                {
                    "start_min": items[idx][0],
                    "end_min": items[idx][1],
                    "cal_id": items[idx][2],
                    "payload": items[idx][3],
                }
                for idx in members
            ]

            cluster_dict: dict[str, Any] = {
                "events": events_data,
                "dominant_index": dominant_local,
                "cluster_start_min": cluster_start_min,
                "cluster_end_min": cluster_end_min,
            }
            result.append((1.0, inner_w, "cluster", cluster_dict))

    return result
