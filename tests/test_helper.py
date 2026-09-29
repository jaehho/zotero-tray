"""The helper's contract: D-Bus on one side, the plugin's pipes on the other.

Runs against a fake StatusNotifierWatcher, so it needs a bus of its own:
`make test` wraps it in dbus-run-session. On a real session the bar already
owns the watcher name, and the suite refuses rather than fight it for it.
"""

import os
import pathlib
import select
import subprocess
import sys
import time
import unittest

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402 — gi.require_version must run first

HELPER = pathlib.Path(__file__).parent.parent / "addon/helper/zotero-tray-helper.py"
WATCHER = "org.kde.StatusNotifierWatcher"
WATCHER_XML = """
<node>
  <interface name="org.kde.StatusNotifierWatcher">
    <method name="RegisterStatusNotifierItem">
      <arg name="service" type="s" direction="in"/>
    </method>
  </interface>
</node>
"""
TOGGLE, QUIT = 1, 3


def bus_call(bus, method, params):
    return bus.call_sync(
        "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
        method, params, None, Gio.DBusCallFlags.NONE, -1, None,
    ).unpack()


def setUpModule():
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    if bus_call(bus, "NameHasOwner", GLib.Variant("(s)", (WATCHER,)))[0]:
        msg = f"{WATCHER} is already owned: this is a real session. Run `make test`."
        raise RuntimeError(msg)


class FakeWatcher:
    def __init__(self, bus):
        self.bus = bus
        self.registered = []
        info = Gio.DBusNodeInfo.new_for_xml(WATCHER_XML).interfaces[0]
        register = (
            getattr(bus, "register_object_with_closures2", None) or bus.register_object
        )
        self.reg_id = register("/StatusNotifierWatcher", info, self.on_call, None, None)
        self.own()

    def on_call(self, _conn, _sender, _path, _iface, _method, params, invocation):
        self.registered.append(params.unpack()[0])
        invocation.return_value(None)

    def own(self):
        bus_call(self.bus, "RequestName", GLib.Variant("(su)", (WATCHER, 4)))

    def disown(self):
        bus_call(self.bus, "ReleaseName", GLib.Variant("(s)", (WATCHER,)))

    def close(self):
        self.disown()
        self.bus.unregister_object(self.reg_id)


class HelperTest(unittest.TestCase):
    watcher_up = True

    def setUp(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.ctx = GLib.MainContext.default()
        self.watcher = FakeWatcher(self.bus)
        if not self.watcher_up:
            self.watcher.disown()
        self.proc = subprocess.Popen(
            [sys.executable, str(HELPER)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0,
        )
        self.name = f"org.kde.StatusNotifierItem-{self.proc.pid}-1"
        self.buf = b""

    def tearDown(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        self.proc.stdin.close()
        self.proc.stdout.close()
        self.watcher.close()

    def pump(self):
        while self.ctx.iteration(False):
            pass

    def line(self, timeout=5):
        """Next line from the helper, serving the fake watcher meanwhile."""
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buf:
            if time.monotonic() > deadline:
                self.fail("helper said nothing")
            self.pump()
            if select.select([self.proc.stdout], [], [], 0.02)[0]:
                chunk = os.read(self.proc.stdout.fileno(), 4096)
                if not chunk:
                    self.fail("helper closed stdout")
                self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode()

    def say(self, message):
        self.proc.stdin.write(message.encode() + b"\n")

    def call(self, path, iface, method, params=None, reply=None):
        # Async, so the helper can be answered while the fake watcher, in this
        # same process, keeps serving.
        result = []
        self.bus.call(
            self.name, path, iface, method, params, reply,
            Gio.DBusCallFlags.NONE, 2000, None,
            lambda conn, res: result.append(conn.call_finish(res)),
        )
        deadline = time.monotonic() + 5
        while not result:
            if time.monotonic() > deadline:
                self.fail(f"{method} never returned")
            self.ctx.iteration(True)
        return result[0].unpack() if result[0] is not None else None

    def menu(self, method, params=None, reply=None):
        return self.call("/MenuBar", "com.canonical.dbusmenu", method, params, reply)

    def click(self, item_id):
        self.menu(
            "Event", GLib.Variant("(isvu)", (item_id, "clicked", GLib.Variant("s", ""), 0))
        )

    def toggle_label(self):
        _rev, (_id, _props, children) = self.menu(
            "GetLayout", GLib.Variant("(iias)", (0, -1, [])), GLib.VariantType("(u(ia{sv}av))")
        )
        for child_id, props, _grandchildren in children:
            if child_id == TOGGLE:
                return props["label"]
        self.fail("no toggle item")
        return None


class WithTray(HelperTest):
    def test_registers_its_own_name(self):
        self.assertEqual(self.line(), "ready")
        self.assertEqual(self.watcher.registered, [self.name])

    def test_item_properties(self):
        self.line()
        props = self.call(
            "/StatusNotifierItem", "org.freedesktop.DBus.Properties", "GetAll",
            GLib.Variant("(s)", ("org.kde.StatusNotifierItem",)),
        )[0]
        self.assertEqual(props["Id"], "zotero")
        self.assertEqual(props["IconName"], "zotero")
        self.assertEqual(props["Menu"], "/MenuBar")

    def test_click_is_relayed(self):
        self.line()
        self.call("/StatusNotifierItem", "org.kde.StatusNotifierItem", "Activate",
                  GLib.Variant("(ii)", (0, 0)))
        self.assertEqual(self.line(), "activate")

    def test_menu_follows_window_state(self):
        self.line()
        self.assertEqual(self.toggle_label(), "Hide Zotero")
        self.click(TOGGLE)
        self.assertEqual(self.line(), "hide")

        self.say("hidden")
        deadline = time.monotonic() + 5
        while self.toggle_label() != "Show Zotero":
            if time.monotonic() > deadline:
                self.fail("label never flipped")
        self.click(TOGGLE)
        self.assertEqual(self.line(), "show")

    def test_quit(self):
        self.line()
        self.click(QUIT)
        self.assertEqual(self.line(), "quit")

    def test_reregisters_when_the_bar_restarts(self):
        self.assertEqual(self.line(), "ready")
        self.watcher.disown()
        self.assertEqual(self.line(), "unavailable")
        self.watcher.own()
        self.assertEqual(self.line(), "ready")
        self.assertEqual(self.watcher.registered, [self.name, self.name])

    def test_exits_when_zotero_does(self):
        self.line()
        self.proc.stdin.close()
        self.assertEqual(self.proc.wait(timeout=5), 0)


class WithoutTray(HelperTest):
    watcher_up = False

    def test_says_unavailable(self):
        self.assertEqual(self.line(), "unavailable")


if __name__ == "__main__":
    unittest.main()
