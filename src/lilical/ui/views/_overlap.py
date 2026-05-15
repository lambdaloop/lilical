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
"""

from __future__ import annotations

from typing import Any


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
