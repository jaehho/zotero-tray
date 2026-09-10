# TODO

## Now

- [ ] Publish: GitHub repo `jaehho/zotero-tray`, then a release carrying the
      .xpi and `updates.json`. `manifest.json`'s `update_url` already points at
      `releases/latest/download/updates.json`; until it exists, Zotero's update
      check just fails quietly.

## Notes

- Not an AUR package on purpose. Zotero never scans `/usr/lib/zotero/extensions`
  (`extensions.startupScanScopes` is 0), which is why `zotero-extension-scihub`
  installs there and never loads. A plugin reaches Zotero through Install
  Plugin From File, and updates through `update_url`.
- Unbuilt: focusing a re-shown window on compositors other than Hyprland
  (GNOME, KDE, sway) needs an xdg-activation token or that compositor's IPC.
