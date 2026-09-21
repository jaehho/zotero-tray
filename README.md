# zotero-tray

Close Zotero to a tray icon instead of quitting. Linux only.

Zotero has no tray on Linux, and the Firefox runtime under it has no API for
one. With this plugin, closing the window hides it, a tray icon brings it back,
and Quit (tray menu or Ctrl+Q) still quits.

## Install

Needs `python3` with PyGObject (`python-gobject` on Arch, `python3-gi` on
Debian/Ubuntu) and a StatusNotifierItem tray: waybar, KDE, or GNOME with the
AppIndicator extension.

Download the `.xpi` from the
[latest release](https://github.com/jaehho/zotero-tray/releases/latest) (or
`make xpi` for `build/`), then in Zotero: Tools → Plugins → gear menu →
Install Plugin From File. Zotero updates it from the release after that.

## Behavior

- **Close** (title bar, Alt+F4, the compositor's close) hides the window, but
  only while a tray is running. With none, close quits as usual.
- **Tray click** shows and focuses Zotero, or hides it if it already has focus.
- **Tray menu**: Show/Hide Zotero, Quit Zotero.
- **Running `zotero` again**, or opening a `zotero://` link, shows the window.
- If the tray goes away while Zotero is hidden, the window comes back.

## Preferences

Settings → Advanced → Config Editor:

| Pref | Default | |
|---|---|---|
| `extensions.zotero-tray.startHidden` | `false` | Hide as soon as the tray is up, for launching at login |
| `extensions.zotero-tray.icon` | `zotero` | Icon theme name; `zotero-symbolic` suits a monochrome bar |

## How it works

`addon/bootstrap.js` cancels the window's `close` event and hides the GTK
window, so it drops out of the compositor's window list instead of parking on a
hidden workspace. The icon is `addon/helper/zotero-tray-helper.py`, a
StatusNotifierItem and dbusmenu that the plugin spawns and talks to over pipes;
the line protocol is in its docstring.

Wayland gives a re-shown window no focus without an activation token, so on
Hyprland the plugin focuses it with `hyprctl`. Elsewhere, focus is up to the
compositor.

`make test` checks the helper's D-Bus side against a fake tray on a private
bus. Tested with Zotero 10.0.1 on Hyprland 0.56.

For Windows, see
[minimize-zotero-to-tray](https://github.com/B3000Kcn/minimize-zotero-to-tray),
whose close interception this follows.

## Releasing and packaging

A release is: bump `version` in `manifest.json`, commit, `make release`.

Not an AUR package on purpose. Zotero never scans `/usr/lib/zotero/extensions`
(`extensions.startupScanScopes` is 0), which is why `zotero-extension-scihub`
installs there and never loads. A plugin reaches Zotero through Install Plugin
From File, and updates through `update_url`.
