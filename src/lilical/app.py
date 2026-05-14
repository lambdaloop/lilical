from __future__ import annotations

import asyncio
import sys

import qasync
from PySide6.QtWidgets import QApplication

from lilical.backends.factory import build_backend_factory
from lilical.config import Config
from lilical.logging_setup import setup_logging
from lilical.recurrence.expander import RecurrenceExpander
from lilical.storage.db import ensure_schema, open_engine
from lilical.storage.event_store import EventStore
from lilical.storage.secrets import SecretsStore
from lilical.sync.engine import SyncEngine
from lilical.ui.main_window import MainWindow
from lilical.ui.notifications import NotificationScheduler

# Monkey-patch qasync 0.28.0 _SimpleTimer.timerEvent to suppress
# benign KeyError when a timer fires after its callback was cleaned up.
_orig_timer_event = qasync._SimpleTimer.timerEvent


def _patched_timer_event(self, event):
    try:
        return _orig_timer_event(self, event)
    except KeyError:
        pass


qasync._SimpleTimer.timerEvent = _patched_timer_event


def main() -> int:
    setup_logging()

    config = Config.load()
    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName("lilical")
    qt_app.setOrganizationName("lilical")
    qt_app.setDesktopFileName("io.github.lilical.Lilical")

    loop = qasync.QEventLoop(qt_app)
    asyncio.set_event_loop(loop)

    db_engine = open_engine(config.db_path)
    ensure_schema(db_engine)

    secrets = SecretsStore.open(config)
    event_store = EventStore(db_engine)
    recurrence = RecurrenceExpander(event_store)
    backend_factory = build_backend_factory(secrets)
    sync_engine = SyncEngine(event_store, secrets, backend_factory)
    notifier = NotificationScheduler(event_store, recurrence)

    window = MainWindow(
        config=config,
        event_store=event_store,
        sync_engine=sync_engine,
        recurrence=recurrence,
        secrets=secrets,
    )
    window.show()

    async def _shutdown() -> None:
        await sync_engine.stop_all()
        await notifier.stop()
        loop.stop()

    qt_app.aboutToQuit.connect(lambda: asyncio.ensure_future(_shutdown()))

    asyncio.ensure_future(sync_engine.start_all())
    asyncio.ensure_future(notifier.start())

    with loop:
        return loop.run_forever() or 0
