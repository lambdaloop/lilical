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

build:
	flatpak-builder \
		--force-clean \
		--repo=$(REPO_DIR) \
		--arch=$(ARCH) \
		$(BUILD_DIR) $(MANIFEST)

install: build
	flatpak-builder \
		--user \
		--install \
		--force-clean \
		--repo=$(REPO_DIR) \
		--arch=$(ARCH) \
		$(BUILD_DIR) $(MANIFEST)

run:
	@flatpak info $(APP_ID) >/dev/null 2>&1 || { \
	  echo "Flatpak not installed — run 'make install' first"; exit 1; }
	flatpak run $(APP_ID)

bundle: build
	flatpak build-bundle $(REPO_DIR) $(APP_ID).flatpak $(APP_ID)

appstream-validate:
	appstreamcli validate ../data/org.lilical.Lilical.metainfo.xml

clean:
	rm -rf $(BUILD_DIR) $(REPO_DIR) .flatpak-builder *.flatpak
