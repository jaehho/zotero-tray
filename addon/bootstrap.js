/* Zotero Tray: close Zotero to a tray icon instead of quitting (Linux).
 *
 * The tray icon itself lives in a helper process (helper/zotero-tray-helper.py),
 * because Gecko has no tray API on Linux. The helper talks StatusNotifierItem
 * over D-Bus and this plugin over a pair of pipes; see its docstring for the
 * line protocol. Hiding unmaps the window through GTK, so it leaves the
 * compositor's window list entirely rather than parking it somewhere.
 */

var ZoteroTray;

const PREF_BRANCH = "extensions.zotero-tray.";
const HELPER = "zotero-tray-helper.py";
const RESTART_DELAY_MS = 2000;
const MAX_RESTARTS = 3;

const { Subprocess } = ChromeUtils.importESModule(
  "resource://gre/modules/Subprocess.sys.mjs"
);

function log(msg) {
  Zotero.debug(`Zotero Tray: ${msg}`);
}

function pref(name) {
  return Zotero.Prefs.get(PREF_BRANCH + name, true);
}

function baseWindow(win) {
  return win.docShell.treeOwner.QueryInterface(Ci.nsIBaseWindow);
}

async function run(command, args) {
  const proc = await Subprocess.call({
    command: await Subprocess.pathSearch(command),
    arguments: args,
    stderr: "stdout",
  });
  let out = "";
  let chunk;
  while ((chunk = await proc.stdout.readString())) out += chunk;
  const { exitCode } = await proc.wait();
  return { out, exitCode };
}

class Tray {
  constructor(rootURI, appStartup) {
    this.rootURI = rootURI;
    // startHidden is for launching Zotero, not for enabling the plugin.
    this.startedHidden = !appStartup;
    this.listeners = new Map();
    this.proc = null;
    this.ready = false;
    this.hidden = false;
    this.quitting = false;
    this.stopping = false;
    this.restarts = 0;
  }

  async start() {
    Services.obs.addObserver(this, "quit-application-granted");
    for (const win of Zotero.getMainWindows()) this.attach(win);

    // Running `zotero` again, or opening a zotero:// link, reaches the live
    // instance here. Zotero only calls focus() on the window, which cannot
    // bring back an unmapped one.
    const tray = this;
    this.ingest = Zotero.CommandLineIngester.ingest;
    Zotero.CommandLineIngester.ingest = function (...args) {
      if (tray.hidden) tray.show();
      return tray.ingest.apply(this, args);
    };

    await this.launchHelper();
  }

  stop() {
    this.stopping = true;
    Zotero.CommandLineIngester.ingest = this.ingest;
    Services.obs.removeObserver(this, "quit-application-granted");
    for (const win of [...this.listeners.keys()]) this.detach(win);
    if (this.hidden) this.show();
    if (this.proc) {
      // EOF on stdin is the helper's cue to exit; kill is the backstop.
      const proc = this.proc;
      this.proc = null;
      proc.stdin.close().catch(() => {});
      proc.kill(1000).catch(() => {});
    }
  }

  observe(_subject, topic) {
    if (topic === "quit-application-granted") this.quitting = true;
  }

  // -- windows ---------------------------------------------------------------

  attach(win) {
    if (this.listeners.has(win)) return;
    // The window manager's close request (title bar, Alt+F4, killactive)
    // arrives as a cancelable "close" event. Quitting does not fire it.
    const listener = (event) => {
      if (this.quitting || !this.ready) return;
      event.preventDefault();
      event.stopPropagation();
      this.hide();
    };
    win.addEventListener("close", listener);
    this.listeners.set(win, listener);
    this.maybeStartHidden();
  }

  detach(win) {
    const listener = this.listeners.get(win);
    if (!listener) return;
    win.removeEventListener("close", listener);
    this.listeners.delete(win);
  }

  hide() {
    const win = Zotero.getMainWindow();
    // Without a tray host there would be no way back to the window.
    if (!win || !this.ready) return;
    baseWindow(win).visibility = false;
    this.hidden = true;
    this.send("hidden");
  }

  async show() {
    const win = Zotero.getMainWindow();
    if (!win) return;
    baseWindow(win).visibility = true;
    this.hidden = false;
    this.send("visible");
    win.focus();
    // A remapped window doesn't take focus on Wayland: there is no input
    // event to hang an activation token on. Ask the compositor directly.
    if (Services.env.get("HYPRLAND_INSTANCE_SIGNATURE")) {
      await this.focusHyprland(win).catch((e) => log(`hyprland focus: ${e}`));
    }
  }

  activate() {
    const win = Zotero.getMainWindow();
    if (!win) return;
    if (!this.hidden && Services.focus.activeWindow === win) this.hide();
    else this.show();
  }

  // At app startup the tray and the main window come up in either order;
  // hide once both exist.
  maybeStartHidden() {
    if (this.startedHidden || !this.ready || !pref("startHidden")) return;
    if (!Zotero.getMainWindow()) return;
    this.startedHidden = true;
    this.hide();
  }

  async focusHyprland(win) {
    const pid = Services.appinfo.processID;
    // The window maps asynchronously after visibility = true, and Hyprland
    // hands it a new address each time, so look it up once it appears.
    for (let i = 0; i < 20; i++) {
      const { out } = await run("hyprctl", ["clients", "-j"]);
      const mine = JSON.parse(out).filter((c) => c.pid === pid);
      const client =
        mine.find((c) => c.title === win.document.title) || mine[0];
      if (client) {
        const target = `address:${client.address}`;
        // Lua configs (0.55+) reject the legacy dispatcher syntax outright.
        let { out: reply } = await run("hyprctl", [
          "dispatch",
          `hl.dsp.focus({ window = "${target}" })`,
        ]);
        if (reply.trim() !== "ok") {
          ({ out: reply } = await run("hyprctl", ["dispatch", "focuswindow", target]));
        }
        if (reply.trim() !== "ok") log(`hyprctl focus: ${reply.trim()}`);
        return;
      }
      await Zotero.Promise.delay(50);
    }
    log("hyprland: window did not map");
  }

  // -- helper ----------------------------------------------------------------

  async launchHelper() {
    let python;
    try {
      python = await Subprocess.pathSearch("python3");
    } catch (e) {
      log("python3 not found; tray disabled");
      return;
    }
    // fetch, not Zotero.File: Zotero's HTTP layer chokes on the jar: URI an
    // installed .xpi is served from.
    const src = await (await fetch(this.rootURI + "helper/" + HELPER)).text();
    const dir = PathUtils.join(PathUtils.profileDir, "zotero-tray");
    await IOUtils.makeDirectory(dir, { ignoreExisting: true });
    const script = PathUtils.join(dir, HELPER);
    await IOUtils.writeUTF8(script, src);

    const proc = await Subprocess.call({
      command: python,
      arguments: [script, "--icon", pref("icon") || "zotero"],
      stderr: "pipe",
    });
    this.proc = proc;
    log(`helper started (pid ${proc.pid})`);
    this.pump(proc.stdout, (line) => this.onMessage(line));
    this.pump(proc.stderr, (line) => log(`helper: ${line}`));
    proc.wait().then(({ exitCode }) => this.onHelperExit(proc, exitCode));
  }

  async pump(pipe, onLine) {
    let buf = "";
    let chunk;
    try {
      while ((chunk = await pipe.readString())) {
        buf += chunk;
        let i;
        while ((i = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, i).trim();
          buf = buf.slice(i + 1);
          if (line) onLine(line);
        }
      }
    } catch (e) {
      // The pipe closes when the helper exits; onHelperExit takes it from there.
    }
  }

  send(state) {
    if (this.proc) this.proc.stdin.write(state + "\n").catch(() => {});
  }

  onMessage(msg) {
    switch (msg) {
      case "ready":
        this.ready = true;
        this.restarts = 0;
        this.send(this.hidden ? "hidden" : "visible");
        this.maybeStartHidden();
        break;
      case "unavailable":
        this.ready = false;
        if (this.hidden) this.show();
        break;
      case "activate":
        this.activate();
        break;
      case "show":
        this.show();
        break;
      case "hide":
        this.hide();
        break;
      case "quit":
        Zotero.Utilities.Internal.quit();
        break;
      default:
        log(`unknown message from helper: ${msg}`);
    }
  }

  onHelperExit(proc, exitCode) {
    if (proc !== this.proc) return;
    this.proc = null;
    this.ready = false;
    if (this.hidden) this.show();
    if (this.stopping || this.quitting) return;
    if (++this.restarts > MAX_RESTARTS) {
      log(`helper exited (${exitCode}) ${MAX_RESTARTS} times; giving up`);
      return;
    }
    log(`helper exited (${exitCode}); restarting`);
    setTimeout(() => {
      if (!this.stopping && !this.quitting) this.launchHelper();
    }, RESTART_DELAY_MS);
  }
}

function install() {}
function uninstall() {}

async function startup({ rootURI }, reason) {
  ZoteroTray = new Tray(rootURI, reason === APP_STARTUP);
  await ZoteroTray.start();
}

function shutdown() {
  ZoteroTray?.stop();
  ZoteroTray = null;
}

function onMainWindowLoad({ window }) {
  ZoteroTray?.attach(window);
}

function onMainWindowUnload({ window }) {
  ZoteroTray?.detach(window);
}
