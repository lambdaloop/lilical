from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def local_iana_tz() -> str:
    """Return the system's IANA timezone name.

    `datetime.now().astimezone().tzinfo` may yield a fixed-offset
    `datetime.timezone` without a `.key` attribute rather than a
    `zoneinfo.ZoneInfo`.  Fall through a chain of OS hints before
    defaulting to "UTC".
    """
    tz = datetime.now().astimezone().tzinfo
    name = getattr(tz, "key", None)
    if name:
        return name
    try:
        with open("/etc/timezone") as f:
            candidate = f.read().strip()
        if candidate:
            ZoneInfo(candidate)
            return candidate
    except (OSError, ZoneInfoNotFoundError):
        pass
    try:
        link = os.readlink("/etc/localtime")
        if "zoneinfo/" in link:
            candidate = link.split("zoneinfo/", 1)[1]
            ZoneInfo(candidate)
            return candidate
    except (OSError, ValueError, ZoneInfoNotFoundError):
        pass
    return "UTC"


def local_zoneinfo() -> ZoneInfo:
    try:
        return ZoneInfo(local_iana_tz())
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")
