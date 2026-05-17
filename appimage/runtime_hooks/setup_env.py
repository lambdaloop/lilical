import os

# PyInstaller bundles a conda-forge libfontconfig.so.1 whose compiled-in
# default config prefix points inside the conda env (which doesn't exist
# at _MEIPASS extraction time). Redirect it to the host's standard
# config tree so the bundled lib finds /etc/fonts/fonts.conf.
os.environ.setdefault("FONTCONFIG_PATH", "/etc/fonts")
