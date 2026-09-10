# zotero-tray - close Zotero to a tray icon instead of quitting.

REPO := jaehho/zotero-tray
VERSION := $(shell jq -r .version addon/manifest.json)
XPI := build/zotero-tray-$(VERSION).xpi

.PHONY: help xpi test lint release clean

help:            ## this list
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) \
	  | sed 's/:.*## /\t/' | expand -t18

xpi: $(XPI)      ## build the plugin into build/

$(XPI): $(shell find addon -type f -not -path '*/__pycache__/*')
	mkdir -p build
	rm -f $@
	cd addon && zip -qrX ../$@ . -x '*/__pycache__/*'

test:            ## the helper's D-Bus contract, on a private session bus
	dbus-run-session -- python -m unittest discover -s tests -v

lint:            ## ruff
	ruff check addon/helper tests

# Zotero polls manifest.json's update_url, releases/latest/download/updates.json,
# so every release carries the .xpi and an updates.json naming its hash.
UPDATE = .applications.zotero as $$z | {addons: {($$z.id): {updates: [{version, update_link: $$link, update_hash: $$hash, applications: {zotero: ($$z | {strict_min_version, strict_max_version})}}]}}}

build/updates.json: $(XPI)
	jq --arg link "https://github.com/$(REPO)/releases/download/v$(VERSION)/$(notdir $(XPI))" \
	  --arg hash "sha256:$$(sha256sum $(XPI) | cut -d' ' -f1)" \
	  '$(UPDATE)' addon/manifest.json > $@

release: build/updates.json  ## tag v$(VERSION) and publish it as a GitHub release
	@git diff --quiet HEAD || { echo "commit first: the .xpi is built from the tree"; exit 1; }
	git tag v$(VERSION)
	git push origin main v$(VERSION)
	gh release create v$(VERSION) --verify-tag --title v$(VERSION) --notes "" $(XPI) build/updates.json

clean:
	rm -rf build
