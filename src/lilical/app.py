from __future__ import annotations

import asyncio
import signal
import sys

import qasync  # type: ignore[reportMissingTypeStubs]
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from lilical.backends.factory import build_backend_factory
from lilical.config import Config
from lilical.logging_setup import setup_logging
from lilical.recurrence.expander import RecurrenceExpander
from lilical.storage.contact_store import ContactStore
from lilical.storage.db import ensure_schema, open_engine
from lilical.storage.event_store import EventStore
from lilical.storage.secrets import SecretsStore
from lilical.sync.engine import SyncEngine
from lilical.ui.main_window import MainWindow
from lilical.ui.notifications import NotificationScheduler

# Monkey-patch qasync 0.28.0 _SimpleTimer.timerEvent to suppress
# benign KeyError when a timer fires after its callback was cleaned up.
_orig_timer_event = qasync._SimpleTimer.timerEvent  # type: ignore[reportPrivateUsage]


def _patched_timer_event(self, event):
    try:
        return _orig_timer_event(self, event)
    except KeyError:
        pass


qasync._SimpleTimer.timerEvent = _patched_timer_event  # type: ignore[reportPrivateUsage]


def main() -> int:
    setup_logging()

    config = Config.load()
    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName("lilical")
    qt_app.setOrganizationName("lilical")
    qt_app.setDesktopFileName("org.lilical.Lilical")

    # Apply UI scale before any widgets are created so every widget (including
    # those built inside MainWindow.__init__) starts with the right font.
    from PySide6.QtCore import QSettings
    from PySide6.QtGui import QFont

    from lilical.ui import theme as _ui_theme

    _s = QSettings()
    _raw = float(_s.value("ui_scale", 1.0) or 1.0)  # type: ignore[reportArgumentType]
    _scale = _raw if _raw in _ui_theme.UI_SCALE_PRESETS else 1.0
    _ui_theme.apply_all_scales(_scale)
    qt_app.setFont(QFont(_ui_theme.FONT_FAMILY, _ui_theme.ui_base_font_pt()))

    loop = qasync.QEventLoop(qt_app)
    asyncio.set_event_loop(loop)

    db_engine = open_engine(config.db_path)
    ensure_schema(db_engine)

    secrets = SecretsStore.open(config)
    event_store = EventStore(db_engine)
    contact_store = ContactStore(db_engine)
    event_store.contacts = contact_store
    recurrence = RecurrenceExpander(event_store)
    backend_factory = build_backend_factory(secrets, event_store)
    sync_engine = SyncEngine(event_store, secrets, backend_factory)
    notifier = NotificationScheduler(event_store, recurrence)

    window = MainWindow(
        config=config,
        event_store=event_store,
        sync_engine=sync_engine,
        recurrence=recurrence,
        secrets=secrets,
        backend_factory=backend_factory,
    )
    window.show()

    async def _shutdown() -> None:
        await sync_engine.stop_all()
        await notifier.stop()
        loop.stop()

    qt_app.aboutToQuit.connect(lambda: asyncio.ensure_future(_shutdown()))

    # Wire Ctrl+C (SIGINT) to a clean shutdown. Two pieces are required:
    # 1. A Python signal handler that calls QApplication.quit() — which fires
    #    aboutToQuit and routes through _shutdown().
    # 2. A no-op QTimer that fires every 100 ms so the Python interpreter gets
    #    a chance to run signal handlers while Qt is in its C++ event loop;
    #    otherwise SIGINT is queued but never delivered until the next Qt event.
    signal.signal(signal.SIGINT, lambda *_: qt_app.quit())
    signal.signal(signal.SIGTERM, lambda *_: qt_app.quit())
    sigint_pulse = QTimer()
    sigint_pulse.start(100)
    sigint_pulse.timeout.connect(lambda: None)

    asyncio.ensure_future(sync_engine.start_all())
    asyncio.ensure_future(notifier.start())

    with loop:
        return loop.run_forever() or 0
