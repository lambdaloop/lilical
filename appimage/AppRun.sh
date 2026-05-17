#!/bin/bash
set -e
HERE="$(dirname "$(readlink -f "$0")")"
export APPDIR="$HERE"
# Prepend bundled libs so we don't pick up host Qt by accident
export LD_LIBRARY_PATH="$HERE/opt/lilical/_internal${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
# Point Qt at the bundled platform plugins
export QT_QPA_PLATFORM_PLUGIN_PATH="$HERE/opt/lilical/_internal/PySide6/Qt/plugins/platforms"
# Point WebEngine at its bundled helper process and resource files
# (conda-forge layout: these live outside PySide6's own tree)
export QTWEBENGINEPROCESS_PATH="$HERE/opt/lilical/_internal/PySide6/Qt/libexec/QtWebEngineProcess"
export QTWEBENGINE_RESOURCES_PATH="$HERE/opt/lilical/_internal/PySide6/Qt/resources"
export QTWEBENGINE_LOCALES_PATH="$HERE/opt/lilical/_internal/PySide6/Qt/translations/qtwebengine_locales"
# Point fontconfig at the bundled fonts + config so Qt resolves sans-serif/monospace correctly
export FONTCONFIG_FILE="$HERE/etc/fonts/fonts.conf"
export FONTCONFIG_PATH="$HERE/etc/fonts"
exec "$HERE/opt/lilical/lilical" "$@"
