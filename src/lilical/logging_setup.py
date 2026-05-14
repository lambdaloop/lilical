from __future__ import annotations

import logging
import os
import sys


def setup_logging() -> None:
    level = os.environ.get("LILICAL_LOG_LEVEL", "INFO")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        from systemd.journal import JournalHandler

        handlers.append(JournalHandler(SYSLOG_IDENTIFIER="lilical"))
    except ImportError:
        pass
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )

    # At DEBUG, also surface HTTP traffic from libraries our backends use, so
    # CalDAV / Graph / Google failures can be diagnosed by URL+status. The
    # python-caldav library uses requests/urllib3; httpx is used directly by
    # our discovery helper and by msal/google clients.
    if level.upper() == "DEBUG":
        for name in ("caldav", "urllib3.connectionpool", "httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.DEBUG)
