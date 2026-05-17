ROOT          := ..
TOOLS_DIR     := tools
APPIMAGETOOL  := $(TOOLS_DIR)/appimagetool-x86_64.AppImage
APPIMAGETOOL_URL := https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
VERSION       ?= $(shell git -C $(ROOT) describe --tags --always 2>/dev/null | sed 's/^v//' || echo dev)
OUTPUT        := lilical-$(VERSION)-x86_64.AppImage
APPDIR        := AppDir
PIXI          := pixi

OAUTH_FILE := $(ROOT)/src/lilical/_oauth_embedded.py
DATA_FILES := $(ROOT)/data/io.github.lilical.Lilical.desktop \
              $(ROOT)/data/io.github.lilical.Lilical.svg \
              $(ROOT)/data/io.github.lilical.Lilical.metainfo.xml
PY_SOURCES := $(shell find $(ROOT)/src -name '*.py' \
                -not -path '*/__pycache__/*' \
                -not -name '_oauth_embedded.py' \
                -not -name '_version.py' 2>/dev/null)
MIGRATIONS := $(shell find $(ROOT)/migrations -type f \
                -not -path '*/__pycache__/*' 2>/dev/null)
STYLES     := $(shell find $(ROOT)/src/lilical/ui/styles -type f 2>/dev/null)
PIXI_FONTS := $(ROOT)/.pixi/envs/appimage/fonts
# All NotoSans*-VF.ttf script variants except Display/Mono (~61 files, ~14.5 MB).
# Requires `pixi install -e appimage` before make runs (wildcard resolves at parse time).
NOTO_SANS_FILES := $(filter-out \
    $(PIXI_FONTS)/NotoSansDisplay-VF.ttf \
    $(PIXI_FONTS)/NotoSansDisplay-Italic-VF.ttf \
    $(PIXI_FONTS)/NotoSansMono-VF.ttf, \
    $(wildcard $(PIXI_FONTS)/NotoSans*-VF.ttf))
FONT_FILES := $(NOTO_SANS_FILES) \
              $(PIXI_FONTS)/Ubuntu-R.ttf \
              $(PIXI_FONTS)/Ubuntu-B.ttf \
              $(PIXI_FONTS)/Ubuntu-RI.ttf \
              $(PIXI_FONTS)/Ubuntu-BI.ttf \
              $(PIXI_FONTS)/UbuntuMono-R.ttf \
              $(PIXI_FONTS)/UbuntuMono-B.ttf \
              $(PIXI_FONTS)/DejaVuSans.ttf \
              $(PIXI_FONTS)/NotoColorEmoji.ttf

.PHONY: all tools embed-oauth pyinstaller appdir build run clean

all: build

# ── Phony aliases → file targets ───────────────────────────────────────────
embed-oauth: $(OAUTH_FILE)
pyinstaller: dist/lilical/lilical
appdir:      $(APPDIR)/AppRun
build:       $(OUTPUT)

# ── tools ──────────────────────────────────────────────────────────────────
tools: $(APPIMAGETOOL)

$(APPIMAGETOOL):
	mkdir -p $(TOOLS_DIR)
	curl -L --output $@ $(APPIMAGETOOL_URL)
	chmod +x $@

# ── OAuth credentials ───────────────────────────────────────────────────────
$(OAUTH_FILE): $(ROOT)/tools/generate_oauth_embedded.py \
               $(wildcard $(ROOT)/oauth_credentials.json)
	$(PIXI) run -e appimage python $(ROOT)/tools/generate_oauth_embedded.py

# ── PyInstaller bundle ──────────────────────────────────────────────────────
dist/lilical/lilical: lilical.spec $(OAUTH_FILE) \
                      $(PY_SOURCES) $(MIGRATIONS) $(STYLES) \
                      $(ROOT)/alembic.ini
	$(PIXI) run -e appimage python -m PyInstaller \
		--distpath dist \
		--workpath build \
		--noconfirm \
		lilical.spec
	rm -rf dist/lilical/_internal/migrations/versions/__pycache__

# ── AppDir assembly ─────────────────────────────────────────────────────────
$(APPDIR)/AppRun: dist/lilical/lilical AppRun.sh fonts.conf $(DATA_FILES) $(FONT_FILES)
	rm -rf $(APPDIR)
	mkdir -p $(APPDIR)/opt \
	         $(APPDIR)/usr/bin \
	         $(APPDIR)/usr/share/applications \
	         $(APPDIR)/usr/share/icons/hicolor/scalable/apps \
	         $(APPDIR)/usr/share/metainfo \
	         $(APPDIR)/usr/share/fonts/lilical \
	         $(APPDIR)/etc/fonts
	cp -r dist/lilical $(APPDIR)/opt/lilical
	ln -sf ../../opt/lilical/lilical $(APPDIR)/usr/bin/lilical
	install -m 755 AppRun.sh $(APPDIR)/AppRun
	cp $(ROOT)/data/io.github.lilical.Lilical.desktop \
	   $(APPDIR)/usr/share/applications/io.github.lilical.Lilical.desktop
	cp $(ROOT)/data/io.github.lilical.Lilical.svg \
	   $(APPDIR)/usr/share/icons/hicolor/scalable/apps/io.github.lilical.Lilical.svg
	cp $(ROOT)/data/io.github.lilical.Lilical.metainfo.xml \
	   $(APPDIR)/usr/share/metainfo/io.github.lilical.Lilical.metainfo.xml
	cp $(ROOT)/data/io.github.lilical.Lilical.desktop $(APPDIR)/io.github.lilical.Lilical.desktop
	cp $(ROOT)/data/io.github.lilical.Lilical.svg $(APPDIR)/io.github.lilical.Lilical.svg
	ln -sf io.github.lilical.Lilical.svg $(APPDIR)/.DirIcon
	cp $(FONT_FILES) $(APPDIR)/usr/share/fonts/lilical/
	cp fonts.conf $(APPDIR)/etc/fonts/fonts.conf
	touch $(APPDIR)/AppRun

# ── AppImage packaging ──────────────────────────────────────────────────────
$(OUTPUT): $(APPDIR)/AppRun $(APPIMAGETOOL)
	ARCH=x86_64 $(APPIMAGETOOL) $(APPDIR) $(OUTPUT)

# ── Smoke test ──────────────────────────────────────────────────────────────
run:
	@test -f $(OUTPUT) || { echo "AppImage missing — run 'make build' first"; exit 1; }
	@echo "Extracting AppImage for headless smoke test..."
	rm -rf squashfs-root
	./$(OUTPUT) --appimage-extract > /dev/null
	@echo "Checking WebEngine + GL assets..."
	@for f in \
	    squashfs-root/opt/lilical/_internal/libOpenGL.so.0 \
	    squashfs-root/opt/lilical/_internal/libEGL.so.1 \
	    squashfs-root/opt/lilical/_internal/libGL.so.1 \
	    squashfs-root/opt/lilical/_internal/tzdata/zoneinfo/UTC \
	    squashfs-root/usr/share/fonts/lilical/Ubuntu-R.ttf \
	    squashfs-root/usr/share/fonts/lilical/DejaVuSans.ttf \
	    squashfs-root/usr/share/fonts/lilical/NotoSans-VF.ttf \
	    squashfs-root/usr/share/fonts/lilical/NotoColorEmoji.ttf \
	    squashfs-root/etc/fonts/fonts.conf; do \
	    if [ ! -e "$$f" ]; then echo "MISSING: $$f"; exit 1; fi; \
	done
	@echo "Bundle assets OK"
	@echo "Running --version (no Qt) to validate Python bootstrap..."
	@out=$$(./squashfs-root/AppRun --version 2>&1); code=$$?; \
	  echo "$$out"; \
	  if [ $$code -ne 0 ]; then \
	    echo "VERSION CHECK FAILED — exit $$code"; exit 1; \
	  fi; \
	  if ! echo "$$out" | grep -qE '^lilical [0-9]+\.[0-9]+\.[0-9]+'; then \
	    echo "VERSION CHECK FAILED — stdout did not match 'lilical X.Y.Z...'"; exit 1; \
	  fi; \
	  echo "Version check passed"
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
	rm -f $(OAUTH_FILE)
