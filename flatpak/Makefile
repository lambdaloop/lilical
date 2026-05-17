APP_ID     := org.lilical.Lilical
MANIFEST   := org.lilical.Lilical.yml
BUILD_DIR  := build-dir
REPO_DIR   := repo
ARCH       := x86_64
RUNTIME    := org.kde.Platform//6.10
# flatpak-pip-generator needs pip3, which lives in the Sdk not the Platform
GENERATOR_RUNTIME := org.kde.Sdk//6.10

GENERATOR  := tools/flatpak-pip-generator
GENERATOR_DEPS := requirements-parser packaging PyYAML

BUILD_STAMP  := .build-stamp
LOCAL_REMOTE := lilical-local

SOURCES := $(MANIFEST) python3-modules.yml \
           ../alembic.ini \
           ../data/$(APP_ID).desktop \
           ../data/$(APP_ID).metainfo.xml \
           ../data/$(APP_ID).svg \
           ../tools/generate_oauth_embedded.py \
           $(wildcard ../oauth_credentials.json) \
           $(shell find ../src -type f -name '*.py' \
                   -not -path '*/__pycache__/*' 2>/dev/null) \
           $(shell find ../migrations -type f \
                   -not -path '*/__pycache__/*' 2>/dev/null)

.PHONY: all deps build install run bundle appstream-validate clean

all: deps build

deps: python3-modules.yml

python3-modules.yml: requirements.txt $(GENERATOR)
	uv run --no-project $(addprefix --with ,$(GENERATOR_DEPS)) \
		$(GENERATOR) \
		--requirements-file=$< \
		--runtime=$(GENERATOR_RUNTIME) \
		--ignore-installed mako,packaging \
		--prefer-wheels cffi,cryptography,dbus-fast,greenlet,lxml,markupsafe,pyyaml,qh3,regex \
		--yaml \
		--wheel-arches=$(ARCH) \
		--output=$(basename $@)
	python3 tools/collapse-modules.py $(basename $@).yaml $@
	rm $(basename $@).yaml

build: $(BUILD_STAMP)

$(BUILD_STAMP): $(SOURCES)
	flatpak-builder \
		--force-clean \
		--repo=$(REPO_DIR) \
		--arch=$(ARCH) \
		$(BUILD_DIR) $(MANIFEST)
	@touch $@

install: $(BUILD_STAMP)
	flatpak --user remote-add --if-not-exists --no-gpg-verify \
		$(LOCAL_REMOTE) $(abspath $(REPO_DIR))
	flatpak --user install -y --noninteractive --reinstall \
		$(LOCAL_REMOTE) $(APP_ID)

run:
	@flatpak info $(APP_ID) >/dev/null 2>&1 || { \
	  echo "Flatpak not installed — run 'make install' first"; exit 1; }
	flatpak run $(APP_ID)

bundle: $(APP_ID).flatpak

$(APP_ID).flatpak: $(BUILD_STAMP)
	flatpak build-bundle $(REPO_DIR) $@ $(APP_ID)

appstream-validate:
	appstreamcli validate ../data/$(APP_ID).metainfo.xml

clean:
	-flatpak --user remote-delete --force $(LOCAL_REMOTE) 2>/dev/null
	rm -rf $(BUILD_DIR) $(REPO_DIR) .flatpak-builder *.flatpak $(BUILD_STAMP)
