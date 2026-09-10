# zotero-tray - close Zotero to a tray icon instead of quitting.

VERSION := $(shell jq -r .version addon/manifest.json)
XPI := build/zotero-tray-$(VERSION).xpi

.PHONY: help xpi test lint clean

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

clean:
	rm -rf build
