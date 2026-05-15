ROOT          := ..
TOOLS_DIR     := tools
APPIMAGETOOL  := $(TOOLS_DIR)/appimagetool-x86_64.AppImage
APPIMAGETOOL_URL := https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
OUTPUT        := lilical-x86_64.AppImage
APPDIR        := AppDir
PIXI          := pixi

.PHONY: all tools embed-oauth pyinstaller appdir build run clean

all: build

# ── tools ──────────────────────────────────────────────────────────────────
tools: $(APPIMAGETOOL)

$(APPIMAGETOOL):
	mkdir -p $(TOOLS_DIR)
	curl -L --output $@ $(APPIMAGETOOL_URL)
	chmod +x $@

# ── OAuth credentials ───────────────────────────────────────────────────────
embed-oauth:
	$(PIXI) run -e appimage python $(ROOT)/tools/generate_oauth_embedded.py

# ── PyInstaller bundle ──────────────────────────────────────────────────────
pyinstaller: embed-oauth
	$(PIXI) run -e appimage python -m PyInstaller \
		--distpath dist \
		--workpath build \
		--noconfirm \
		lilical.spec

# ── AppDir assembly ─────────────────────────────────────────────────────────
appdir: pyinstaller
	rm -rf $(APPDIR)
	mkdir -p $(APPDIR)/opt \
	         $(APPDIR)/usr/bin \
	         $(APPDIR)/usr/share/applications \
	         $(APPDIR)/usr/share/icons/hicolor/scalable/apps \
	         $(APPDIR)/usr/share/metainfo
	# PyInstaller onedir output
	cp -r dist/lilical $(APPDIR)/opt/lilical
	# usr/bin symlink → opt bundle
	ln -sf ../../opt/lilical/lilical $(APPDIR)/usr/bin/lilical
	# AppRun launcher
	install -m 755 AppRun.sh $(APPDIR)/AppRun
	# Desktop metadata
	cp $(ROOT)/data/io.github.lilical.Lilical.desktop \
	   $(APPDIR)/usr/share/applications/io.github.lilical.Lilical.desktop
	cp $(ROOT)/data/io.github.lilical.Lilical.svg \
	   $(APPDIR)/usr/share/icons/hicolor/scalable/apps/io.github.lilical.Lilical.svg
	cp $(ROOT)/data/io.github.lilical.Lilical.metainfo.xml \
	   $(APPDIR)/usr/share/metainfo/io.github.lilical.Lilical.metainfo.xml
	# appimagetool requires a .desktop and icon at the AppDir root
	cp $(ROOT)/data/io.github.lilical.Lilical.desktop $(APPDIR)/io.github.lilical.Lilical.desktop
	cp $(ROOT)/data/io.github.lilical.Lilical.svg $(APPDIR)/io.github.lilical.Lilical.svg
	ln -sf io.github.lilical.Lilical.svg $(APPDIR)/.DirIcon

# ── AppImage packaging ──────────────────────────────────────────────────────
build: appdir $(APPIMAGETOOL)
	ARCH=x86_64 $(APPIMAGETOOL) $(APPDIR) $(OUTPUT)

# ── Smoke test ──────────────────────────────────────────────────────────────
run: build
	@echo "Extracting AppImage for headless smoke test..."
	rm -rf squashfs-root
	./$(OUTPUT) --appimage-extract > /dev/null
	@echo "Running with offscreen Qt platform (5-second timeout)..."
	QT_QPA_PLATFORM=offscreen timeout 5 ./squashfs-root/AppRun 2>&1 | tee /tmp/lilical-smoke.log; \
	  code=$$?; \
	  if grep -qiE "importerror|modulenotfounderror|cannot open shared object|qt platform plugin" /tmp/lilical-smoke.log; then \
	    echo "SMOKE TEST FAILED — see /tmp/lilical-smoke.log"; exit 1; \
	  else \
	    echo "Smoke test passed (exit $$code, no fatal import/lib errors)"; \
	  fi

clean:
	rm -rf $(APPDIR) build dist __pycache__ $(OUTPUT) squashfs-root
	rm -f $(ROOT)/src/lilical/_oauth_embedded.py
