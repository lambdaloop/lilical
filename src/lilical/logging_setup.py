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
