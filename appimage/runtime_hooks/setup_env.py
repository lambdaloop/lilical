import os
import sys

# Point bundled libfontconfig at the bundled fonts.conf so Qt resolves
# "sans-serif" to Noto Sans (matching the AppImage and pixi dev env).
# fonts.conf <include>s /etc/fonts/conf.d and the user's xdg config so
# hinting/antialiasing/subpixel preferences from the host still apply.
meipass = getattr(sys, "_MEIPASS", None)
if meipass:
    os.environ.setdefault("FONTCONFIG_FILE", os.path.join(meipass, "etc", "fonts", "fonts.conf"))
    os.environ.setdefault("FONTCONFIG_PATH", os.path.join(meipass, "etc", "fonts"))
