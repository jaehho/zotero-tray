# TODO

## Now

- [x] Published 2026-09-10: `github.com/jaehho/zotero-tray`, release v0.1.0.
      The next one is: bump `version` in `manifest.json`, commit,
      `make release`.

## Notes

- Not an AUR package on purpose. Zotero never scans `/usr/lib/zotero/extensions`
  (`extensions.startupScanScopes` is 0), which is why `zotero-extension-scihub`
  installs there and never loads. A plugin reaches Zotero through Install
  Plugin From File, and updates through `update_url`.
- Unbuilt: focusing a re-shown window on compositors other than Hyprland
  (GNOME, KDE, sway) needs an xdg-activation token or that compositor's IPC.
