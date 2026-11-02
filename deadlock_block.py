
import ctypes
import ipaddress
import json
import os
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

APP_ID = 1422450
URL = f"https://api.steampowered.com/ISteamApps/GetSDRConfig/v1/?appid={APP_ID}"
PREFIX = "dlblock_"
CHUNK = 100  # IPs per rule, netsh chokes on long command lines
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# No Russian relay, so players from Russia get routed to one of these.
# Blocking only one just moves them to the next.
LINKED = ["frankfurt", "stockholm", "helsinki"]
LINKED_NOTE = (
    "Frankfurt, Stockholm and Helsinki are linked: there is no Russian relay, so "
    "Russian players land on any of them. Blocking one just moves them to another."
)

# codes from the Deadlock+ "EU All" preset, the rest is matched by city/country name
EU_CODES = {"frankfurt", "ams", "lhr", "par", "mad", "stockholm", "vie", "dvie", "waw"}
EU_WORDS = [
    "frankfurt", "germany", "stockholm", "sweden", "helsinki", "finland", "amsterdam", "netherlands",
    "london", "england", "paris", "france", "madrid", "spain", "vienna", "austria", "warsaw", "poland",
    "milan", "italy",
]
# Guam is left out on purpose, it is nowhere near the mainland
US_CODES = {"atl", "ord", "dfw", "lax", "sea", "iad", "ewr", "mia", "sjc", "den", "phx", "slc", "ord2", "iad2"}
US_WORDS = [
    "atlanta", "chicago", "dallas", "los angeles", "seattle", "sterling", "(virginia)", "(illinois)",
    "(texas)", "(california)", "(washington)", "new york", "new jersey", "miami", "florida",
    "san jose", "denver", "colorado", "phoenix", "arizona", "salt lake", "(utah)", "(oregon)",
]
PRESETS = {
    "us": "open only the USA, block everything else",
    "eu": "open only Europe, block everything else",
    "no-eu": "block every European region",
}


CACHE_NAME = "sdr_config.json"
SOURCE = {"text": ""}  # shown in the status line


def cache_paths():
    here = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
    paths = [os.path.join(here, CACHE_NAME)]
    appdata = os.environ.get("APPDATA")
    if appdata:
        paths.append(os.path.join(appdata, "DeadlockBlocker", CACHE_NAME))
    bundled = getattr(sys, "_MEIPASS", None)  # copy packed into the exe by build_exe.bat
    if bundled:
        paths.append(os.path.join(bundled, CACHE_NAME))
    return paths


def save_cache(raw):
    for path in cache_paths()[:2]:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(raw)
            return
        except OSError:
            continue


def load_cache():
    for path in cache_paths():
        try:
            with open(path, "rb") as f:
                return json.loads(f.read()), path
        except (OSError, ValueError):
            continue
    return None, None


def fetch_pops():
    req = urllib.request.Request(URL, headers={"User-Agent": "deadlock-block/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
        data = json.loads(raw)
        save_cache(raw)
        SOURCE["text"] = ""
    except Exception as e:  # blocked, offline, no proxy
        data, path = load_cache()
        if data is None:
            raise RuntimeError(
                f"Cannot reach Steam API ({e}). Turn the proxy/VPN on and press Refresh once, "
                f"or put {CACHE_NAME} next to the program."
            ) from e
        SOURCE["text"] = " (saved list, Steam API unreachable)"
    pops = {}
    for code, pop in data.get("pops", {}).items():
        desc = pop.get("desc")
        ips = []
        for relay in pop.get("relays") or []:
            raw = relay.get("ipv4")
            if raw and ipaddress.ip_address(raw).is_global:  # never block LAN/loopback
                ips.append(raw)
        if desc and ips:
            pops[code] = (desc, sorted(set(ips)))
    return pops


def is_europe(code, desc):
    d = desc.lower()
    return code.lower() in EU_CODES or any(w in d for w in EU_WORDS)


def is_usa(code, desc):
    d = desc.lower()
    return code.lower() in US_CODES or any(w in d for w in US_WORDS)


def is_linked(desc):
    d = desc.lower()
    return any(w in d for w in LINKED)


def linked_codes(pops):
    return {c for c, (d, _) in pops.items() if is_linked(d)}


def expand_linked(pops, codes):
    """If any chosen region is linked, add all of them. -> (codes, added, missing keywords)"""
    codes = set(codes)
    if not any(is_linked(pops[c][0]) for c in codes):
        return codes, set(), []
    every = linked_codes(pops)
    absent = [w for w in LINKED if not any(w in d.lower() for d, _ in pops.values())]
    return codes | every, every - codes, absent


def preset_target(pops, name):
    if name == "us":
        return set(pops) - {c for c, (d, _) in pops.items() if is_usa(c, d)}
    europe = {c for c, (d, _) in pops.items() if is_europe(c, d)}
    return set(pops) - europe if name == "eu" else europe


def pending(pops, current, target):
    """(code, desc, ips, want_blocked) for everything that differs between current and target"""
    out = []
    for code in sorted(pops, key=lambda c: pops[c][0].lower()):
        if (code in target) != (code in current):
            desc, ips = pops[code]
            out.append((code, desc, ips, code in target))
    return out


def netsh(*args, dry=False):
    cmd = ["netsh", "advfirewall", "firewall", *args]
    if dry:
        print(" ".join(cmd))
        return 0
    return subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW).returncode


def rule_name(code):
    return f"{PREFIX}{code}"


def is_blocked(code):
    # Return code only, so it works on any Windows language.
    return netsh("show", "rule", f"name={rule_name(code)}") == 0


def blocked_now(pops):
    codes = list(pops)
    with ThreadPoolExecutor(max_workers=8) as pool:
        flags = list(pool.map(is_blocked, codes))
    return {c for c, f in zip(codes, flags) if f}


def block(code, ips, dry=False):
    netsh("delete", "rule", f"name={rule_name(code)}", dry=dry)
    for i in range(0, len(ips), CHUNK):
        rc = netsh(
            "add", "rule", f"name={rule_name(code)}", "dir=out", "action=block",
            f"remoteip={','.join(ips[i:i + CHUNK])}", dry=dry,
        )
        if rc != 0:
            return False
    return True


def unblock(code, dry=False):
    return netsh("delete", "rule", f"name={rule_name(code)}", dry=dry) == 0


def apply_changes(changes, dry=False):
    """-> descriptions of the changes that failed"""
    failed = []
    for code, desc, ips, want in changes:
        ok = block(code, ips, dry) if want else unblock(code, dry)
        if not dry:
            print(f"{'ok  ' if ok else 'FAIL'} {'block' if want else 'unblock'} {desc}")
        if not ok:
            failed.append(desc)
    return failed


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except AttributeError:
        return False


def pick(pops, words):
    words = [w.lower() for w in words]
    return {c for c, (d, _) in pops.items() if any(w == c.lower() or w in d.lower() for w in words)}


def cli(argv):
    dry = "--dry-run" in argv
    use_linked = "--no-linked" not in argv
    args = [a for a in argv if not a.startswith("--")]
    cmd, words = args[0], args[1:]
    if cmd not in ("list", "block", "unblock", "preset"):
        print(__doc__)
        return 2
    if cmd != "list" and not dry and not is_admin():
        print("Run this from an Administrator terminal.")
        return 1

    pops = fetch_pops()

    if cmd == "list":
        current = set() if dry else blocked_now(pops)
        for code, (desc, ips) in sorted(pops.items(), key=lambda kv: kv[1][0].lower()):
            tags = ("EU " if is_europe(code, desc) else "   ") + ("L" if is_linked(desc) else " ")
            print(f"{'X' if code in current else ' '} {tags} {code:9} {desc} ({len(ips)} IPs)")
        print("\nX = blocked, EU = Europe, L = linked region")
        return 0

    if cmd == "preset":
        name = words[0] if words else ""
        if name not in PRESETS:
            print("Presets: " + "; ".join(f"{k} ({v})" for k, v in PRESETS.items()))
            return 2
        current = set() if dry else blocked_now(pops)
        changes = pending(pops, current, preset_target(pops, name))
        if not changes:
            print("Already in that state.")
            return 0
        return 1 if apply_changes(changes, dry) else 0

    if not words:
        print("Name at least one region, e.g. `block london`.")
        return 2

    if cmd == "unblock" and words == ["all"]:
        targets = set(pops)
    else:
        targets = pick(pops, words)
    if not targets:
        print("No region matches. Try `list`.")
        return 1

    if cmd == "block" and use_linked:
        targets, added, absent = expand_linked(pops, targets)
        if added or absent:
            print(LINKED_NOTE)
        if added:
            print("Also blocking: " + ", ".join(sorted(pops[c][0] for c in added)))
        if absent:
            print("No relay in Valve's data, cannot be blocked: " + ", ".join(w.title() for w in absent))

    changes = [(c, pops[c][0], pops[c][1], cmd == "block") for c in sorted(targets, key=lambda c: pops[c][0].lower())]
    return 1 if apply_changes(changes, dry) else 0


def gui():
    try:
        from PySide6.QtCore import Qt, QThread, Signal
        from PySide6.QtWidgets import (
            QApplication, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu,
            QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
        )
    except ImportError:
        print("The window needs Qt6 for Python:  pip install PySide6")
        return 1

    CHECKED, UNCHECKED = Qt.CheckState.Checked, Qt.CheckState.Unchecked

    class Task(QThread):
        done = Signal(object)
        failed = Signal(str)

        def __init__(self, fn):
            super().__init__()
            self.fn = fn

        def run(self):
            try:
                self.done.emit(self.fn())
            except Exception as e:
                self.failed.emit(str(e))

    def load_all():
        pops = fetch_pops()
        return pops, blocked_now(pops)

    class Window(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("Deadlock server blocker")
            self.resize(680, 680)
            self.pops, self.initial = {}, set()
            self.task = None

            root = QWidget()
            self.setCentralWidget(root)
            layout = QVBoxLayout(root)

            self.banner = QWidget()
            row = QHBoxLayout(self.banner)
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(QLabel("Administrator rights are needed to change firewall rules."), 1)
            elevate = QPushButton("Restart as admin")
            elevate.clicked.connect(self.restart_as_admin)
            row.addWidget(elevate)
            self.banner.setVisible(not is_admin())
            layout.addWidget(self.banner)

            top = QHBoxLayout()
            self.filter = QLineEdit(placeholderText="Filter regions (e.g. london)")
            self.filter.setClearButtonEnabled(True)
            self.filter.textChanged.connect(self.apply_filter)
            top.addWidget(self.filter, 1)
            self.preset_btn = QPushButton("Presets")
            menu = QMenu(self.preset_btn)
            menu.addAction("Only USA (block the rest)", lambda: self.use_preset("us"))
            menu.addAction("Only Europe (block the rest)", lambda: self.use_preset("eu"))
            menu.addAction("Block all of Europe", lambda: self.use_preset("no-eu"))
            self.preset_btn.setMenu(menu)
            top.addWidget(self.preset_btn)
            self.refresh_btn = QPushButton("Refresh")
            self.refresh_btn.clicked.connect(self.reload)
            top.addWidget(self.refresh_btn)
            layout.addLayout(top)

            self.table = QTableWidget(0, 4)
            self.table.setHorizontalHeaderLabels(["Block", "Region", "Linked", "Relay IPs"])
            self.table.verticalHeader().setVisible(False)
            self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
            head = self.table.horizontalHeader()
            head.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            head.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            head.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            head.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
            self.table.itemChanged.connect(self.on_item_changed)
            layout.addWidget(self.table, 1)

            self.note = QLabel("")
            self.note.setWordWrap(True)
            layout.addWidget(self.note)

            bottom = QHBoxLayout()
            check_shown = QPushButton("Check shown")
            check_shown.clicked.connect(self.check_shown)
            clear_all = QPushButton("Clear all")
            clear_all.clicked.connect(lambda: self.set_checked(set()))
            self.status = QLabel("")
            self.apply_btn = QPushButton("Apply")
            self.apply_btn.setDefault(True)
            self.apply_btn.clicked.connect(self.apply)
            bottom.addWidget(check_shown)
            bottom.addWidget(clear_all)
            bottom.addWidget(self.status, 1)
            bottom.addWidget(self.apply_btn)
            layout.addLayout(bottom)

            self.update_apply()
            self.reload()

        def busy(self, text):
            self.status.setText(text)
            for w in (self.refresh_btn, self.preset_btn, self.apply_btn, self.table):
                w.setEnabled(False)

        def idle(self):
            for w in (self.refresh_btn, self.preset_btn, self.table):
                w.setEnabled(True)
            self.update_apply()

        def run_task(self, fn, on_done):
            self.task = Task(fn)
            self.task.done.connect(on_done)
            self.task.failed.connect(self.on_failed)
            self.task.start()

        def on_failed(self, message):
            self.idle()
            self.status.setText("Error: " + message)

        def checked_codes(self):
            out = set()
            for r in range(self.table.rowCount()):
                item = self.table.item(r, 0)
                if item.checkState() == CHECKED:
                    out.add(item.data(Qt.ItemDataRole.UserRole))
            return out

        def set_checked(self, codes):
            self.table.blockSignals(True)
            for r in range(self.table.rowCount()):
                item = self.table.item(r, 0)
                item.setCheckState(CHECKED if item.data(Qt.ItemDataRole.UserRole) in codes else UNCHECKED)
            self.table.blockSignals(False)
            self.note.setText("")
            self.update_apply()

        def reload(self):
            self.busy("Loading servers from Valve and checking firewall rules...")
            self.run_task(load_all, self.on_loaded)

        def on_loaded(self, result):
            self.pops, self.initial = result
            self.table.blockSignals(True)
            self.table.setRowCount(0)
            for code in sorted(self.pops, key=lambda c: self.pops[c][0].lower()):
                desc, ips = self.pops[code]
                r = self.table.rowCount()
                self.table.insertRow(r)
                box = QTableWidgetItem()
                box.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                box.setCheckState(CHECKED if code in self.initial else UNCHECKED)
                box.setData(Qt.ItemDataRole.UserRole, code)
                region = QTableWidgetItem(f"{desc}  [{code}]")
                link = QTableWidgetItem("linked" if is_linked(desc) else "")
                count = QTableWidgetItem(str(len(ips)))
                count.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                for col, item in enumerate((box, region, link, count)):
                    self.table.setItem(r, col, item)
            self.table.blockSignals(False)
            self.apply_filter()
            self.note.setText("")
            self.idle()
            self.status.setText(f"{len(self.pops)} regions, {len(self.initial)} blocked{SOURCE['text']}")

        def apply_filter(self):
            text = self.filter.text().strip().lower()
            for r in range(self.table.rowCount()):
                self.table.setRowHidden(r, bool(text) and text not in self.table.item(r, 1).text().lower())

        def check_shown(self):
            codes = self.checked_codes()
            for r in range(self.table.rowCount()):
                if not self.table.isRowHidden(r):
                    codes.add(self.table.item(r, 0).data(Qt.ItemDataRole.UserRole))
            self.set_checked(codes)

        def use_preset(self, name):
            if self.pops:
                self.set_checked(preset_target(self.pops, name))
                self.status.setText(PRESETS[name].capitalize() + ". Press Apply.")

        def on_item_changed(self, item):
            if item.column() == 0 and item.checkState() == CHECKED:
                code = item.data(Qt.ItemDataRole.UserRole)
                if code in self.pops and is_linked(self.pops[code][0]):
                    codes, added, absent = expand_linked(self.pops, self.checked_codes())
                    if added:
                        self.set_checked(codes)
                        text = LINKED_NOTE + " Also checked: " + ", ".join(sorted(self.pops[c][0] for c in added)) + "."
                        if absent:
                            text += " No relay for " + ", ".join(w.title() for w in absent) + ", it cannot be blocked."
                        self.note.setText(text)
                        return
            self.update_apply()

        def update_apply(self, *_):
            n = len(pending(self.pops, self.initial, self.checked_codes()))
            self.apply_btn.setText(f"Apply ({n})" if n else "Apply")
            self.apply_btn.setEnabled(n > 0 and is_admin() and self.table.isEnabled())
            self.apply_btn.setToolTip("" if is_admin() else "Restart as administrator first")

        def apply(self):
            changes = pending(self.pops, self.initial, self.checked_codes())
            if changes:
                self.busy(f"Applying {len(changes)} change(s)...")
                self.run_task(lambda: apply_changes(changes), self.on_applied)

        def on_applied(self, failed):
            if failed:
                QMessageBox.warning(self, "Some changes failed", "Could not change:\n" + "\n".join(failed))
            self.reload()

        def restart_as_admin(self):
            try:
                args = sys.argv[1:] if getattr(sys, "frozen", False) else sys.argv
                params = subprocess.list2cmdline(args)
                rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
            except AttributeError:
                QMessageBox.information(self, "Not Windows", "Elevation is only available on Windows.")
                return
            if rc > 32:
                QApplication.quit()

    app = QApplication(sys.argv)
    window = Window()
    window.show()
    return app.exec()


def main(argv):
    if not [a for a in argv if not a.startswith("--")] or argv[0] == "gui":
        return gui()
    return cli(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
