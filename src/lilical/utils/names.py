from __future__ import annotations


def format_display_name(name: str | None) -> str | None:
    """Return the name in 'First Last' order.

    Heuristic: a string with exactly one comma and no '@' is treated as
    'Last, First [Middle…]' and the halves are swapped.  All other forms
    (already 'First Last', email addresses, multi-comma strings) pass through
    unchanged.
    """
    if not name:
        return name
    if "@" in name:
        return name
    if name.count(",") != 1:
        return name
    last, first = name.split(",", 1)
    last, first = last.strip(), first.strip()
    if not last or not first:
        return name
    return f"{first} {last}"
