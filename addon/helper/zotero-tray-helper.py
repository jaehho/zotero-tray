"""Tray icon for Zotero, driven over pipes by the zotero-tray plugin.

The plugin spawns this process. It publishes a StatusNotifierItem (the tray
protocol waybar, KDE and most bars speak) with a small dbusmenu, and relays
clicks back. EOF on stdin means Zotero is gone, and ends the process.

    stdin   visible | hidden
    stdout  ready | unavailable | activate | show | hide | quit

`ready` is sent each time a tray host accepts the item, `unavailable` when
none is running, so the plugin only hides the window while it can come back.
"""

import argparse
import os
import sys

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402 — gi.require_version must run first

try:  # GLib 2.84+ moved the fd watch; older PyGObject only has the GLib one
    gi.require_version("GLibUnix", "2.0")
    from gi.repository import GLibUnix

    fd_add_full = GLibUnix.fd_add_full
except (ImportError, ValueError):
    fd_add_full = GLib.unix_fd_add_full

WATCHER = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"

ITEM_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="ContextMenu">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="Activate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="Scroll">
      <arg name="delta" type="i" direction="in"/>
      <arg name="orientation" type="s" direction="in"/>
    </method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus">
      <arg name="status" type="s"/>
    </signal>
  </interface>
</node>
"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg name="parentId" type="i" direction="in"/>
      <arg name="recursionDepth" type="i" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="revision" type="u" direction="out"/>
      <arg name="layout" type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="properties" type="a(ia{sv})" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg name="id" type="i" direction="in"/>
      <arg name="name" type="s" direction="in"/>
      <arg name="value" type="v" direction="out"/>
    </method>
    <method name="Event">
      <arg name="id" type="i" direction="in"/>
      <arg name="eventId" type="s" direction="in"/>
      <arg name="data" type="v" direction="in"/>
      <arg name="timestamp" type="u" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg name="events" type="a(isvu)" direction="in"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg name="id" type="i" direction="in"/>
      <arg name="needUpdate" type="b" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="updatesNeeded" type="ai" direction="out"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg name="updatedProps" type="a(ia{sv})"/>
      <arg name="removedProps" type="a(ias)"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg name="revision" type="u"/>
      <arg name="parent" type="i"/>
    </signal>
    <signal name="ItemActivationRequested">
      <arg name="id" type="i"/>
      <arg name="timestamp" type="u"/>
    </signal>
  </interface>
</node>
"""

TOGGLE, SEPARATOR, QUIT = 1, 2, 3


def s(value):
    return GLib.Variant("s", value)


class Tray:
    def __init__(self, loop, icon, icon_theme_path):
        self.loop = loop
        self.icon = icon
        self.icon_theme_path = icon_theme_path
        self.visible = True
        self.revision = 1
        self.buf = b""

        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        # Requested synchronously: the watcher resolves the name the moment we
        # register, so it has to be ours already.
        self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self.bus.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "RequestName", GLib.Variant("(su)", (self.name, 4)),
            None, Gio.DBusCallFlags.NONE, -1, None,
        )
        for path, xml, on_call, on_prop in (
            (ITEM_PATH, ITEM_XML, self.on_item_call, self.item_prop),
            (MENU_PATH, MENU_XML, self.on_menu_call, self.menu_prop),
        ):
            info = Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0]
            register = (
                getattr(self.bus, "register_object_with_closures2", None)
                or self.bus.register_object
            )
            register(path, info, on_call, on_prop, None)

        # Fires "appeared" again whenever the bar restarts, which is when an
        # item has to re-register; "vanished" fires at once if there is none.
        Gio.bus_watch_name_on_connection(
            self.bus, WATCHER, Gio.BusNameWatcherFlags.NONE,
            self.on_watcher_appeared, self.on_watcher_vanished,
        )
        fd_add_full(
            GLib.PRIORITY_DEFAULT, sys.stdin.fileno(),
            GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR,
            self.on_stdin,
        )

    # -- pipes ---------------------------------------------------------------

    def send(self, message):
        # stdout is the protocol with the extension, so write it, not print
        # it (ruff T20 bans print; a lint pass once turned this into `pass`).
        try:
            sys.stdout.write(message + "\n")
            sys.stdout.flush()
        except BrokenPipeError:
            self.loop.quit()

    def on_stdin(self, fd, _condition):
        data = os.read(fd, 4096)
        if not data:
            self.loop.quit()
            return GLib.SOURCE_REMOVE
        *lines, self.buf = (self.buf + data).split(b"\n")
        for line in lines:
            state = line.decode().strip()
            if state in ("visible", "hidden"):
                self.set_visible(state == "visible")
        return GLib.SOURCE_CONTINUE

    def set_visible(self, visible):
        if visible == self.visible:
            return
        self.visible = visible
        self.revision += 1
        self.bus.emit_signal(
            None, MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated",
            GLib.Variant("(ui)", (self.revision, 0)),
        )

    # -- watcher -------------------------------------------------------------

    def on_watcher_appeared(self, conn, _name, _owner):
        conn.call(
            WATCHER, WATCHER_PATH, WATCHER, "RegisterStatusNotifierItem",
            GLib.Variant("(s)", (self.name,)), None, Gio.DBusCallFlags.NONE,
            -1, None, self.on_registered,
        )

    def on_registered(self, conn, result):
        try:
            conn.call_finish(result)
        except GLib.Error as e:
            sys.stderr.write(f"zotero-tray-helper: register failed: {e.message}\n")
            self.send("unavailable")
            return
        self.send("ready")

    def on_watcher_vanished(self, _conn, _name):
        self.send("unavailable")

    # -- StatusNotifierItem --------------------------------------------------

    def item_prop(self, _conn, _sender, _path, _iface, name):
        return {
            "Category": s("ApplicationStatus"),
            "Id": s("zotero"),
            "Title": s("Zotero"),
            "Status": s("Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconName": s(self.icon),
            "IconThemePath": s(self.icon_theme_path),
            "OverlayIconName": s(""),
            "AttentionIconName": s(""),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", (self.icon, [], "Zotero", "")),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
        }.get(name)

    def on_item_call(self, _conn, _sender, _path, _iface, method, _params, invocation):
        if method in ("Activate", "SecondaryActivate"):
            self.send("activate")
        invocation.return_value(None)

    # -- dbusmenu ------------------------------------------------------------

    def menu_prop(self, _conn, _sender, _path, _iface, name):
        return {
            "Version": GLib.Variant("u", 3),
            "TextDirection": s("ltr"),
            "Status": s("normal"),
            "IconThemePath": GLib.Variant("as", []),
        }.get(name)

    def item_properties(self, item_id):
        if item_id == 0:
            return {"children-display": s("submenu")}
        if item_id == TOGGLE:
            return {"label": s("Hide Zotero" if self.visible else "Show Zotero")}
        if item_id == SEPARATOR:
            return {"type": s("separator")}
        if item_id == QUIT:
            return {"label": s("Quit Zotero")}
        return None

    def layout(self, item_id):
        children = [TOGGLE, SEPARATOR, QUIT] if item_id == 0 else []
        return (
            item_id,
            self.item_properties(item_id),
            [GLib.Variant("(ia{sv}av)", self.layout(child)) for child in children],
        )

    def on_event(self, item_id, event_id):
        if event_id != "clicked":
            return
        if item_id == TOGGLE:
            self.send("hide" if self.visible else "show")
        elif item_id == QUIT:
            self.send("quit")

    def on_menu_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        args = params.unpack()
        if method == "GetLayout":
            parent = args[0] if self.item_properties(args[0]) is not None else 0
            reply = GLib.Variant("(u(ia{sv}av))", (self.revision, self.layout(parent)))
        elif method == "GetGroupProperties":
            ids = args[0] or [TOGGLE, SEPARATOR, QUIT]
            reply = GLib.Variant("(a(ia{sv}))", ([
                (i, self.item_properties(i)) for i in ids
                if self.item_properties(i) is not None
            ],))
        elif method == "GetProperty":
            props = self.item_properties(args[0]) or {}
            if args[1] not in props:
                invocation.return_dbus_error(
                    "com.canonical.dbusmenu.Error", f"no property {args[1]}"
                )
                return
            reply = GLib.Variant("(v)", (props[args[1]],))
        elif method == "Event":
            self.on_event(args[0], args[1])
            reply = None
        elif method == "EventGroup":
            for item_id, event_id, _data, _ts in args[0]:
                self.on_event(item_id, event_id)
            reply = GLib.Variant("(ai)", ([],))
        elif method == "AboutToShow":
            reply = GLib.Variant("(b)", (False,))
        elif method == "AboutToShowGroup":
            reply = GLib.Variant("(aiai)", ([], []))
        else:
            invocation.return_dbus_error(
                "org.freedesktop.DBus.Error.UnknownMethod", method
            )
            return
        invocation.return_value(reply)


def main():
    parser = argparse.ArgumentParser(description="Tray icon for the zotero-tray plugin.")
    parser.add_argument("--icon", default="zotero")
    parser.add_argument("--icon-theme-path", default="")
    args = parser.parse_args()

    loop = GLib.MainLoop()
    Tray(loop, args.icon, args.icon_theme_path)
    loop.run()


if __name__ == "__main__":
    main()
