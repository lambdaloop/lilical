from __future__ import annotations

import shutil
import subprocess
import sys
import webbrowser


def open_url(url: str) -> bool:
    """Open *url* in the user's preferred browser.

    On Linux, prefer ``xdg-open`` so the user's xdg-mime default is honored.
    Inside a Flatpak sandbox the runtime's ``xdg-open`` routes the request
    through the OpenURI portal to the host's default browser.
    """
    if sys.platform.startswith("linux"):
        xdg = shutil.which("xdg-open")
        if xdg is not None:
            try:
                subprocess.Popen(
                    [xdg, url],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=True,
                )
                return True
            except OSError:
                pass
    return webbrowser.open(url)
