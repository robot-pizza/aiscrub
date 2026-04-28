PYTHON_SOURCE := aiscrub.py
BIN_NAME      := aiscrub
VERSION       := $(shell awk -F\" '/^VERSION = /{print $$2}' $(PYTHON_SOURCE))

UNAME_S := $(shell uname -s 2>/dev/null || echo unknown)
UNAME_M := $(shell uname -m 2>/dev/null || echo unknown)

ifeq ($(OS),Windows_NT)
  PLATFORM := windows
  EXE      := .exe
else ifeq ($(UNAME_S),Linux)
  PLATFORM := linux
  EXE      :=
else ifeq ($(UNAME_S),Darwin)
  PLATFORM := macos
  EXE      :=
else
  PLATFORM := $(shell echo $(UNAME_S) | tr A-Z a-z)
  EXE      :=
endif

ARCH      ?= $(UNAME_M)
ASSET     := $(BIN_NAME)-$(PLATFORM)-$(ARCH)

.PHONY: help sync build zip release clean scan dryrun version tag

help:
	@echo "aiscrub Makefile targets:"
	@echo "  make sync        sync uv environment (runtime + PyInstaller)"
	@echo "  make build       build PyInstaller binary into dist/"
	@echo "  make zip         build + package into $(ASSET).zip"
	@echo "  make tag         create git tag v$(VERSION) (no push)"
	@echo "  make release     tag v$(VERSION) AND push tag (triggers GH release workflow)"
	@echo "  make scan        run aiscrub scan from source on the current repo"
	@echo "  make dryrun      run aiscrub scrub (dry-run) on the current repo"
	@echo "  make clean       remove build/, dist/, staging/, *.spec, *.zip"
	@echo "  make version     print the version embedded in $(PYTHON_SOURCE)"

version:
	@echo $(VERSION)

sync:
	uv sync --extra build

build: sync
	uv run pyinstaller --onefile --name $(BIN_NAME) --console $(PYTHON_SOURCE)
	@echo "built: dist/$(BIN_NAME)$(EXE)"

zip: build
	@rm -rf staging
	@mkdir -p staging
	@cp dist/$(BIN_NAME)$(EXE) staging/
	@if [ -f README.md ]; then cp README.md staging/; fi
	@if [ -f LICENSE ];   then cp LICENSE   staging/; fi
	@rm -f $(ASSET).zip
	@cd staging && zip -r ../$(ASSET).zip . > /dev/null
	@echo "built: $(ASSET).zip"

tag:
	@if [ -z "$(VERSION)" ]; then echo "error: VERSION not set" >&2; exit 1; fi
	@if git rev-parse "v$(VERSION)" >/dev/null 2>&1; then \
	    echo "tag v$(VERSION) already exists"; \
	else \
	    git tag -a "v$(VERSION)" -m "v$(VERSION)"; \
	    echo "created tag v$(VERSION) (not pushed; use 'make release' to push)"; \
	fi

release: tag
	git push origin "v$(VERSION)"
	@echo "pushed tag v$(VERSION) — GitHub release workflow should now run"

scan:
	uv run python $(PYTHON_SOURCE) scan

dryrun:
	uv run python $(PYTHON_SOURCE) scrub

clean:
	rm -rf build dist staging *.spec *.zip SHA256SUMS.txt
