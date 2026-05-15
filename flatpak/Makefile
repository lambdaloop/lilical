APP_ID     := io.github.lilical.Lilical
MANIFEST   := io.github.lilical.Lilical.yml
BUILD_DIR  := build-dir
REPO_DIR   := repo
ARCH       := x86_64
RUNTIME    := org.kde.Platform//6.10

GENERATOR  := tools/flatpak-pip-generator
GENERATOR_DEPS := requirements-parser packaging PyYAML

.PHONY: all deps build install run bundle appstream-validate clean

all: deps build

deps: python3-modules.yml

python3-modules.yml: requirements.txt $(GENERATOR)
	uv run --no-project $(addprefix --with ,$(GENERATOR_DEPS)) \
		$(GENERATOR) \
		--requirements-file=$< \
		--runtime=$(RUNTIME) \
		--yaml \
		--wheel-arches=$(ARCH) \
		--output=$@

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

run: install
	flatpak run $(APP_ID)

bundle: build
	flatpak build-bundle $(REPO_DIR) $(APP_ID).flatpak $(APP_ID)

appstream-validate:
	appstreamcli validate ../data/io.github.lilical.Lilical.metainfo.xml

clean:
	rm -rf $(BUILD_DIR) $(REPO_DIR) .flatpak-builder *.flatpak
