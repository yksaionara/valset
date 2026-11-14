"""valset in the terminal: a full-screen, keyboard-driven view of the live match, your
settings saves and options. Lists are picked with the arrow keys and Enter.

Cheap by design: the Riot polling runs in app.Live on a background thread. This loop
sleeps on the console's input handle (no busy polling) and wakes a few times a second
to see whether anything changed; when it did, only the lines that differ are rewritten.
"""
from __future__ import annotations
import ctypes
import os
import queue
import re
import sys
import threading
import time
import unicodedata
from datetime import datetime

import app
import updater
from valset import VERSION, AppError

# ---------------------------------------------------------------------------- colours

C = dict(text="#eef2f6", muted="#8d9aa8", accent="#ff4655", good="#3fd68b", bad="#ff6b76",
         warn="#ffc457", info="#6cc4ff", line="#3a4756", head="#1c2530")
RANK_COLORS = ["#9aa4ad", "#c08a56", "#c9d4dc", "#f0c24f", "#3fc6cc", "#c792f5", "#3fd68b", "#ff6b86", "#ffe27a"]
PARTY_COLORS = ["#ffb020", "#4cc3ff", "#b794ff", "#ff7ac6", "#5fe3a1", "#ff8a65"]
HOTKEYS = [  # presets you can cycle through in Options (Windows virtual-key codes)
    {"ctrl": True, "alt": False, "shift": True, "vk": 0x44, "label": "Ctrl+Shift+D"},
    {"ctrl": True, "alt": True, "shift": False, "vk": 0x44, "label": "Ctrl+Alt+D"},
    {"ctrl": False, "alt": False, "shift": False, "vk": 0x77, "label": "F8"},
    {"ctrl": False, "alt": False, "shift": False, "vk": 0x78, "label": "F9"},
    {"ctrl": False, "alt": False, "shift": False, "vk": 0x79, "label": "F10"},
    {"ctrl": True, "alt": False, "shift": True, "vk": 0x7B, "label": "Ctrl+Shift+F12"},
]
TABS = [("live", "Live match"), ("saves", "Saves"), ("options", "Options")]
RESET, BOLD = "\x1b[0m", "\x1b[1m"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def fg(hexcol):
    h = hexcol.lstrip("#")
    return f"\x1b[38;2;{int(h[0:2], 16)};{int(h[2:4], 16)};{int(h[4:6], 16)}m"


def bgc(hexcol):
    h = hexcol.lstrip("#")
    return f"\x1b[48;2;{int(h[0:2], 16)};{int(h[2:4], 16)};{int(h[4:6], 16)}m"


def paint(text, color=None, bold=False, bg=None):
    return (BOLD if bold else "") + (fg(color) if color else "") + (bgc(bg) if bg else "") + str(text) + RESET


def cw(ch):
    """Terminal cells a character takes (wide Asian characters take two)."""
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def vlen(s):
    return sum(cw(ch) for ch in ANSI.sub("", s))


def fit(text, width):
    """Plain text cut (with …) or padded to exactly `width` cells."""
    text, out, used = str(text), "", 0
    for ch in text:
        w = cw(ch)
        if used + w > width:
            if width > 0:
                while out and used + 1 > width:
                    used -= cw(out[-1])
                    out = out[:-1]
                out += "…"
                used += 1
            break
        out += ch
        used += w
    return out + " " * max(0, width - used)


def clip(line, width):
    """Cut a coloured line to `width` visible cells, keeping its colour codes intact."""
    out, used, i = [], 0, 0
    while i < len(line):
        m = ANSI.match(line, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        w = cw(line[i])
        if used + w > width:
            break
        out.append(line[i])
        used += w
        i += 1
    return "".join(out) + RESET


def rank_col(tier):
    return RANK_COLORS[min(8, (tier - 3) // 3)] if tier and tier >= 3 else C["muted"]


# ---------------------------------------------------------------------------- console

class Console:
    STD_INPUT = -10

    def __init__(self):
        self.prev = []
        self.size = (0, 0)
        if os.name == "nt":
            k32 = ctypes.windll.kernel32
            self.hin = k32.GetStdHandle(self.STD_INPUT)
            k32.SetConsoleTitleW("valset")
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[2J")  # own screen, hide cursor
        sys.stdout.flush()

    def close(self):
        sys.stdout.write("\x1b[0m\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()

    def dims(self):
        try:
            s = os.get_terminal_size()
            return max(40, s.columns), max(12, s.lines)
        except OSError:
            return 120, 40

    def draw(self, lines):
        """Write only the lines that changed since the last frame."""
        w, h = self.dims()
        if (w, h) != self.size:
            self.size, self.prev = (w, h), []
            sys.stdout.write("\x1b[2J")
        lines = [clip(line, w - 1) for line in lines[:h]]
        out = []
        for i in range(max(len(lines), len(self.prev))):
            new = lines[i] if i < len(lines) else ""
            if i >= len(self.prev) or self.prev[i] != new:
                out.append(f"\x1b[{i + 1};1H{new}\x1b[K")
        if out:
            sys.stdout.write("".join(out))
            sys.stdout.flush()
        self.prev = lines

    def wait_key(self, timeout):
        """Sleep until a key arrives or `timeout` seconds pass. Returns the key or None."""
        import msvcrt
        if not msvcrt.kbhit():
            ctypes.windll.kernel32.WaitForSingleObject(self.hin, int(timeout * 1000))
        if msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                return {"H": "up", "P": "down", "K": "left", "M": "right", "I": "pgup", "Q": "pgdn",
                        "G": "home", "O": "end", "S": "del"}.get(msvcrt.getwch())
            return ch
        # the input handle also wakes up for mouse/focus events; drop those so we sleep again
        n = ctypes.c_ulong()
        ctypes.windll.kernel32.GetNumberOfConsoleInputEvents(self.hin, ctypes.byref(n))
        if n.value:
            ctypes.windll.kernel32.FlushConsoleInputBuffer(self.hin)
        return None


# ---------------------------------------------------------------------------- the app

class TUI:
    def __init__(self):
        self.prefs = app.load_prefs()
        self.live = app.Live()
        self.hotkey = app.Hotkey(self.live.hotkey_pressed)
        self.apply_hotkey()
        self.tab = "live"
        self.scroll = {t: 0 for t, _ in TABS}
        self.sel = {"saves": 0, "options": 0}  # highlighted row on the list screens
        self.focus_line = None  # body line of the highlighted row, so scrolling follows it
        self.msg, self.msg_until, self.msg_kind = "", 0, "info"
        self.saves = None
        self.busy = ""  # "Saving...", "Loading..." while a save action runs
        self.tasks = queue.Queue()
        self.dialog = None  # {"lines": fn() -> [str], "on_key": fn(key)} while a question is open
        self.dialog_v = 0
        self.con = None
        self.update = None  # newer GitHub release, if there is one
        self.update_state, self.update_msg, self.update_pct = "idle", "", 0.0
        self.restart_after = False
        self.load_saves()
        if self.prefs.get("auto_update", True):
            self.check_update(force=False, quiet=True)

    # ---- helpers

    def apply_hotkey(self):
        if self.prefs.get("hotkey_enabled"):
            self.hotkey.start(self.prefs.get("hotkey"))
        else:
            self.hotkey.stop()
            self.hotkey.error = None

    def set_pref(self, key, value):
        self.prefs[key] = value
        app.save_prefs(self.prefs)
        if key in ("hotkey", "hotkey_enabled"):
            self.apply_hotkey()

    def toast(self, text, kind="info"):
        self.msg, self.msg_kind, self.msg_until = text, kind, time.time() + (9 if kind == "error" else 6)

    def bg(self, fn, on_ok=None, on_err=None):
        def work():
            try:
                self.tasks.put((on_ok, fn(), None))
            except AppError as e:
                self.tasks.put((on_err, None, str(e)))
            except Exception as e:
                self.tasks.put((on_err, None, f"Something went wrong: {e}"))
        threading.Thread(target=work, daemon=True).start()

    def hk_label(self):
        return self.prefs["hotkey"]["label"] if self.prefs.get("hotkey_enabled") else None

    # ---- dialogs: a short question in the footer, answered with one key

    def open_dialog(self, lines, on_key):
        self.dialog = {"lines": lines, "on_key": on_key}
        self.dialog_v += 1

    def close_dialog(self):
        self.dialog = None
        self.dialog_v += 1

    def confirm(self, question, fn, detail="", yes="Enter", danger=False):
        """Yes/no question. `yes` is the key that means yes ("Enter" or "Y"); Esc or any other key cancels."""
        keys = ("\r", "\n") if yes == "Enter" else ("y", "Y")
        col = C["accent"] if danger else C["info"]

        def lines():
            out = [" " + paint(question, C["text"], True)]
            if detail:
                out.append(" " + paint(detail, C["muted"]))
            out.append(" " + paint(f" {yes} ", "#0b0f14", True, col) + paint(" yes     ", C["muted"]) +
                       paint(" Esc ", C["text"], True, C["head"]) + paint(" no", C["muted"]))
            return out

        def on_key(ch):
            self.close_dialog()
            if ch in keys:
                fn()
            else:
                self.toast("Cancelled.", "info")
        self.open_dialog(lines, on_key)

    # ---- updates

    def check_update(self, force=True, quiet=False):
        self.update_state, self.update_msg = "checking", ""

        def done(rel):
            self.update, self.update_state = rel, "idle"
            if rel:
                self.toast(f"valset {rel['tag']} is out. Go to Options (3) to install it.", "info")
            else:
                self.update_msg = "You have the newest version."
                if not quiet:
                    self.toast(self.update_msg, "ok")

        def fail(m):
            self.update_state, self.update_msg = "idle", m
            if not quiet:
                self.toast(m, "error")
        self.bg(lambda: updater.check(force), done, fail)

    def install_update(self):
        rel = self.update
        self.update_state, self.update_pct = "downloading", 0.0

        def progress(p):
            self.update_pct = p

        def done(msg):
            self.update_state, self.update_msg = "done", msg
            if updater.install_kind() == "exe":
                self.restart_after = True  # run() exits and starts the new exe in this window
            else:
                self.toast(msg, "ok")

        def fail(m):
            self.update_state, self.update_msg = "idle", m
            self.toast(m, "error")
        self.bg(lambda: updater.install(rel, progress), done, fail)

    def update_action(self):
        if self.update_state in ("checking", "downloading"):
            return
        if self.update_state == "done":
            return self.toast(self.update_msg, "ok")
        if not self.update:
            self.toast("Checking GitHub for updates...", "info")
            return self.check_update(force=True)
        if updater.install_kind() == "git":
            return self.toast("This folder is a git checkout, so valset won't overwrite it. Run: git pull", "warn")
        restart = "valset restarts by itself when it's done." if updater.install_kind() == "exe" else \
            "Close and reopen valset afterwards."
        self.confirm(f"Install valset {self.update['tag']}?", self.install_update, detail=restart)

    # ---- frame

    def frame(self):
        w, h = self.con.dims()
        st, b = self.live.status, self.live.board
        top = []
        acc = paint("● " + st["account"], C["good"]) if st["riot"] == "ok" else \
            paint("● connecting...", C["muted"]) if st["riot"] == "starting" else paint("● Riot Client not connected", C["bad"])
        game = paint("● VALORANT " + ("open" if st["game_open"] else "closed"), C["good"] if st["game_open"] else C["muted"])
        phase = ("   " + paint("● " + ("Agent select" if b["phase"] == "agent select" else "In a match"), C["warn"], True)) if b else ""
        upd = ("   " + paint(f"⬆ {self.update['tag']} out (Options)", C["info"], True)) \
            if self.update and self.update_state != "done" else ""
        top.append(" " + paint("val", C["text"], True) + paint("set", C["accent"], True) + "   " + acc + "   " + game + phase + upd)
        tabs = []
        for i, (key, label) in enumerate(TABS, 1):
            if key == self.tab:
                tabs.append(paint(f" {i} {label} ", "#0b0f14", True, C["text"]))
            else:
                tabs.append(paint(f" {i} ", C["accent"], True) + paint(f"{label} ", C["muted"]))
        top.append(" " + " ".join(tabs))
        top.append(paint("─" * (w - 1), C["line"]))

        self.focus_line = None
        body = {"live": self.live_lines, "saves": self.saves_lines, "options": self.options_lines}[self.tab](w)

        foot = [paint("─" * (w - 1), C["line"])]
        if self.dialog:
            foot[0] = paint("─" * (w - 1), C["warn"])
            foot += self.dialog["lines"]()
        elif self.busy:
            foot.append(" " + paint(self.busy, C["info"], True))
        elif self.msg and time.time() < self.msg_until:
            col = {"ok": C["good"], "error": C["bad"], "warn": C["warn"]}.get(self.msg_kind, C["info"])
            foot.append(" " + paint(self.msg, col, True))
        else:
            foot.append(" " + self.keys_hint())
        room = max(1, h - len(top) - len(foot))
        max_scroll = max(0, len(body) - room)
        off = self.scroll[self.tab]
        if self.focus_line is not None:  # keep the highlighted row on screen
            if self.focus_line < off + 1:
                off = self.focus_line - 1
            elif self.focus_line >= off + room - 1:
                off = self.focus_line - room + 2
        off = self.scroll[self.tab] = max(0, min(off, max_scroll))
        view = body[off:off + room]
        if max_scroll:
            more = f" {off + 1}-{off + len(view)} of {len(body)} lines "
            foot[0] = paint("─" * (w - 2 - len(more)), C["line"]) + paint(more, C["muted"])
        return top + view + [""] * (room - len(view)) + foot

    def keys_hint(self):
        k = lambda key, label: paint(f" {key} ", C["text"], True, C["head"]) + paint(f" {label}   ", C["muted"])
        common = k("1 2 3", "screens") + k("Q", "quit")
        if self.tab == "live":
            b = self.live.board
            return (k("D", "dodge") if b and b["can_dodge"] else "") + (k("↑↓", "scroll") if b else "") + common
        if self.tab == "saves":
            row = self.save_rows()[self.sel["saves"]] if self.saves else None
            if row is None or row.get("new"):
                return k("↑↓", "pick") + k("Enter", "save my settings") + common
            out = k("↑↓", "pick") + k("Enter", "load") + k("Del", "delete")
            if self.saves.get("can_undo"):
                out += k("U", "undo last load")
            return out + common
        return k("↑↓", "pick") + k("Enter", "change") + common

    # ---- live

    def live_lines(self, w):
        b, st = self.live.board, self.live.status
        if not b:
            if st["riot"] == "starting":
                title, text = "Connecting...", ["Looking for the Riot Client."]
            elif st["riot"] != "ok":
                title, text = "Can't reach your Riot account", [st["message"], "",
                                                                 "valset keeps trying by itself, so this clears up once the Riot Client is signed in."]
            elif not st["game_open"]:
                title, text = "VALORANT is closed", ["Start the game. Your match shows up here the moment you hit agent select."]
            else:
                title, text = "Waiting for a match", ["Queue up. The board fills in by itself when agent select starts."]
            return ["", "", "   " + paint(title, C["text"], True), ""] + ["   " + paint(t, C["muted"]) for t in text]
        out = [""]
        select = b["phase"] == "agent select"
        head = " " + paint(b["map"], C["text"], True) + paint(f"  ·  {b['queue_name']}  ·  ", C["muted"]) + \
            paint("AGENT SELECT" if select else "IN GAME", C["warn"] if select else C["good"], True)
        if b["can_dodge"]:
            hk = self.hk_label()
            right = paint(" D ", "#ffffff", True, C["accent"]) + paint(" dodge" + (f"   or {hk} twice" if hk else ""), C["muted"])
            head += " " * max(3, w - 2 - vlen(head) - vlen(right)) + right
        out.append(head)
        if self.live.armed and self.hk_label():
            out += ["", paint(fit(f"  Press {self.hk_label()} again to dodge", w - 1), "#ffffff", True, C["accent"])]
        narrow = w < 100
        skins = self.prefs.get("show_skins") and not self.prefs.get("compact")
        for t in b["teams"]:
            out.append("")
            col = C["good"] if t["mine"] else C["bad"]
            avg = t["avg_rank"]
            label = f" {t['label'].upper()} "
            right = f" average {avg['name']} " if avg else ""
            out.append(paint("━━", col) + paint(label, col, True) +
                       paint("━" * max(2, w - 5 - len(label) - len(right)), col) + paint(right, rank_col(avg["tier"]) if avg else C["muted"]))
            name_w = max(14, min(30, w - 76))
            if not narrow:
                out.append(paint(f"   {fit('AGENT', 10)}{fit('PLAYER', name_w + 1)}{fit('RANK', 20)}{fit('PEAK', 14)}"
                                 f"{'WIN%':>5} {'K/D':>5} {'HS%':>5}  PARTY", C["muted"]))
            for p in t["players"]:
                out += self.player_lines(p, w, name_w, narrow, skins)
        out += ["", paint(" Party: in a party now, or queued together in their recent games. Shows once their stats load.", C["muted"]),
                paint(" WIN% is this act. K/D and HS% are from their last 3 competitive games.", C["muted"])]
        if select:
            out.append(paint(" The enemy team appears once the match loads.", C["muted"]))
        return out

    def player_lines(self, p, w, name_w, narrow, skins):
        party = p["party"]
        pcol = PARTY_COLORS[(party["n"] - 1) % len(PARTY_COLORS)] if party else None
        stripe = paint("▌", pcol) if pcol else " "
        agent = paint(fit(p["agent"], 10), C["muted"])
        name = p["name"] + (" (you)" if p["me"] else "")
        name_s = paint(fit(name, name_w), C["good"] if p["me"] else C["text"], True)
        if p["loading"]:
            rank = paint(fit("loading...", 19), C["muted"])
            peak = paint(fit("", 13), C["muted"])
        else:
            rk, pk = p["rank"], p["peak"]
            rtxt = (f"{rk['name']} {rk['rr']}rr" if rk["tier"] > 2 else "Unranked") if rk else "?"
            rank = paint(fit(rtxt, 19), rank_col(rk["tier"]) if rk else C["muted"], True)
            peak = paint(fit(pk["name"] if pk and pk["tier"] > 2 else "-", 13), rank_col(pk["tier"]) if pk else C["muted"])

        def stat(v, fmt, good, bad, loading):
            if v is None:
                return paint(f"{'...' if loading else '-':>5}", C["muted"])
            col = C["good"] if good(v) else C["bad"] if bad(v) else C["text"]
            return paint(f"{fmt(v):>5}", col, True)
        wr = stat(p["wr"], lambda v: f"{v}%", lambda v: v >= 55 and (p["games"] or 0) >= 5,
                  lambda v: v < 45 and (p["games"] or 0) >= 5, False)
        kd = stat(p["kd"], lambda v: f"{v:.2f}", lambda v: v >= 1.2, lambda v: v < 0.9, p["stats_loading"])
        hs = stat(p["hs"], lambda v: f"{v}%", lambda v: v >= 28, lambda v: v < 15, p["stats_loading"])
        if party:
            tag = paint(f" {party['label'].upper()} {party['n']} ", "#0b0f14", True, pcol)
        else:
            tag = paint(" solo", C["muted"]) if p["solo"] else ""
        if narrow:
            lines = [f" {stripe} {name_s} {paint(p['agent'], C['muted'])}  {tag}",
                     f" {stripe}   {rank} {paint('peak', C['muted'])} {peak} "
                     f"{paint('WR', C['muted'])}{wr} {paint('KD', C['muted'])}{kd} {paint('HS', C['muted'])}{hs}"]
        else:
            lines = [f" {stripe} {agent}{name_s} {rank} {peak} {wr} {kd} {hs}  {tag}"]
        if skins and p["skins"]:
            sk = "   ".join(f"{k['weapon']}: {k['name']}" for k in p["skins"])
            lines.append(f" {stripe}" + paint(("   " if narrow else " " * 12) + sk, C["muted"]))
        return lines

    # ---- saves: a list you move through with ↑↓; Enter saves or loads

    def load_saves(self):
        def done(d):
            self.saves = d
            self.sel["saves"] = min(self.sel["saves"], len(self.save_rows()) - 1)
        self.bg(app.saves_data, done)

    def save_rows(self):
        """Selectable rows: 'save my settings' first, then this account's saves, other accounts', backups."""
        d = self.saves or {"saves": []}
        mine = [x for x in d["saves"] if x["mine"] and not x["auto"]]
        other = [x for x in d["saves"] if not x["mine"] and not x["auto"]]
        auto = [x for x in d["saves"] if x["auto"]]
        return [{"new": True}] + mine + other + auto

    def row_line(self, selected, text, w):
        """A list row; the highlighted one gets a bar and a ▶ marker."""
        if selected:
            return paint("▶", C["accent"], True) + paint(" " + text + " " * max(0, w - 3 - vlen(text)), None, False, C["head"])
        return "  " + text

    def saves_lines(self, w):
        d = self.saves
        if not d:
            return ["", "   " + paint("Loading your saves...", C["muted"])]
        out = [""]
        if d["account"]:
            out.append(" " + paint("Your settings saves", C["text"], True) + paint(f"   signed in as {d['account']}", C["muted"]))
        else:
            out.append(" " + paint("Sign in to the Riot Client to save or load settings.", C["warn"], True))
        if self.live.status.get("game_open"):
            out.append(" " + paint("Close VALORANT before loading. The game overwrites your settings when it closes.", C["warn"]))
        rows = self.save_rows()
        sel = self.sel["saves"] = max(0, min(self.sel["saves"], len(rows) - 1))
        out.append("")
        if sel == 0:
            self.focus_line = len(out)
        out.append(self.row_line(sel == 0, paint("+ Save my current settings", C["good"], True) +
                                 paint("   sens, keybinds, crosshair, audio, video...", C["muted"]), w))
        sections = [("THIS ACCOUNT", lambda x: x["mine"] and not x["auto"]),
                    ("OTHER ACCOUNTS", lambda x: not x["mine"] and not x["auto"]),
                    ("AUTOMATIC BACKUPS", lambda x: x["auto"])]
        name_w = max(16, min(34, w - 50))
        for title, test in sections:
            items = [(i, x) for i, x in enumerate(rows) if i and test(x)]
            if not items:
                if title == "THIS ACCOUNT" and d["account"]:
                    out += ["", "   " + paint("No saves for this account yet. Press Enter on the line above.", C["muted"])]
                continue
            out += ["", " " + paint(title, C["muted"], True)]
            for i, x in items:
                when = ""
                try:
                    when = datetime.fromisoformat(x["created"]).strftime("%b %d, %H:%M")
                except (TypeError, ValueError):
                    pass
                extra = "" if x["mine"] else f"from {x['account']}   "
                text = paint(fit(x["label"], name_w), C["text"], True) + "  " + \
                    paint(extra + when + ("   + video" if x["has_video"] else ""), C["muted"])
                if i == sel:
                    self.focus_line = len(out)
                out.append(self.row_line(i == sel, text, w))
        if d.get("can_undo"):
            out += ["", " " + paint("Loaded the wrong thing? Press U to put this account back how it was.", C["muted"])]
        return out

    def saves_key(self, c):
        d = self.saves
        if not d or self.busy:
            return
        rows = self.save_rows()
        if c in ("up", "down", "home", "end", "pgup", "pgdn"):
            step = {"up": -1, "down": 1, "home": -999, "end": 999, "pgup": -10, "pgdn": 10}[c]
            self.sel["saves"] = max(0, min(len(rows) - 1, self.sel["saves"] + step))
            return
        row = rows[self.sel["saves"]]
        if c in ("\r", "\n", " "):
            return self.save_new() if row.get("new") else self.load_save(row)
        if c in ("del", "x", "\x08") and not row.get("new"):
            return self.confirm(f"Delete \"{row['label']}\"?", lambda: self.run_action("Deleting...", lambda: app.do_delete(row["id"])),
                                detail="This can't be undone.", yes="Y", danger=True)
        if c == "u" and d.get("can_undo"):
            return self.confirm(f"Undo the last load on {d['account']}?",
                                lambda: self.run_action("Undoing...", app.do_undo),
                                detail="Puts this account's settings back to how they were before you loaded.")

    def run_action(self, busy_text, fn):
        self.busy = busy_text

        def done(msg):
            self.busy = ""
            self.toast(msg, "ok")
            self.load_saves()

        def fail(m):
            self.busy = ""
            self.toast(m, "error")
            self.load_saves()
        self.bg(fn, done, fail)

    def save_new(self):
        d = self.saves
        if not d["account"]:
            return self.toast("Sign in to the Riot Client first.", "warn")
        label = self.line_input(f"Name this save and press Enter (just Enter = \"{d['account']}\", Esc = cancel): ")
        if label is None:
            return self.toast("Cancelled.", "info")
        self.run_action("Saving your settings...", lambda: app.do_save(label))

    def load_save(self, x):
        d = self.saves
        if not d["account"]:
            return self.toast("Sign in to the Riot Client first.", "warn")
        if self.live.status.get("game_open"):
            return self.toast("Close VALORANT first. The game overwrites your settings when it closes.", "warn")
        parts = [c for c, _ in d["parts"]]

        def go(chosen):
            self.close_dialog()
            self.run_action("Loading...", lambda: app.do_load(x["id"], chosen))

        def lines():
            return [" " + paint(f"Load \"{x['label']}\" onto {d['account']}?", C["text"], True),
                    " " + paint("Loads everything. Your current settings are backed up first, so U can undo it.", C["muted"]),
                    " " + paint(" Enter ", "#0b0f14", True, C["info"]) + paint(" load     ", C["muted"]) +
                    paint(" P ", C["text"], True, C["head"]) + paint(" choose parts     ", C["muted"]) +
                    paint(" Esc ", C["text"], True, C["head"]) + paint(" cancel", C["muted"])]

        def on_key(ch):
            if ch in ("\r", "\n"):
                go(parts)
            elif isinstance(ch, str) and ch.lower() == "p":
                self.pick_parts(x, go)
            else:
                self.close_dialog()
                self.toast("Cancelled.", "info")
        self.open_dialog(lines, on_key)

    def pick_parts(self, x, go):
        names = self.saves["parts"]  # [(category, readable name)]
        on = {c: True for c, _ in names}

        def lines():
            boxes = "   ".join(paint(f"{i}", C["accent"], True) + " " +
                               (paint("■ " + n, C["text"], True) if on[c] else paint("□ " + n, C["muted"]))
                               for i, (c, n) in enumerate(names, 1))
            return [" " + paint(f"Which parts of \"{x['label']}\"?", C["text"], True) +
                    paint("   press a number to tick or untick it", C["muted"]),
                    " " + boxes,
                    " " + paint(" Enter ", "#0b0f14", True, C["info"]) + paint(" load the ticked parts     ", C["muted"]) +
                    paint(" Esc ", C["text"], True, C["head"]) + paint(" cancel", C["muted"])]

        def on_key(ch):
            if ch in ("\r", "\n"):
                chosen = [c for c, _ in names if on[c]]
                if not chosen:
                    return self.toast("Tick at least one part.", "warn")
                go(chosen)
            elif isinstance(ch, str) and ch.isdigit() and 1 <= int(ch) <= len(names):
                c = names[int(ch) - 1][0]
                on[c] = not on[c]
                self.dialog_v += 1
            elif ch == "\x1b":
                self.close_dialog()
                self.toast("Cancelled.", "info")
        self.open_dialog(lines, on_key)

    # ---- options: same list style, Enter changes the highlighted setting

    def option_rows(self):
        p = self.prefs
        hk = p["hotkey"]["label"]
        if self.update_state == "checking":
            upd = paint("checking GitHub...", C["muted"])
        elif self.update_state == "downloading":
            n = int(self.update_pct * 20)
            upd = paint("█" * n, C["info"]) + paint("░" * (20 - n), C["line"]) + paint(f" {self.update_pct * 100:3.0f}%", C["muted"])
        elif self.update_state == "done":
            upd = paint(self.update_msg, C["good"], True)
        elif self.update:
            upd = paint(f"{self.update['tag']} is out", C["info"], True)
        else:
            upd = paint(self.update_msg or f"you have v{VERSION}", C["muted"])
        return [
            ("Compact board", p.get("compact"), "one line per player",
             lambda: self.set_pref("compact", not p.get("compact"))),
            ("Show skins", p.get("show_skins"), "Vandal, Phantom and knife under each player (when compact is off)",
             lambda: self.set_pref("show_skins", not p.get("show_skins"))),
            ("Dodge hotkey", p.get("hotkey_enabled"), f"press {hk} twice in agent select to dodge, even in game",
             lambda: self.set_pref("hotkey_enabled", not p.get("hotkey_enabled"))),
            ("Hotkey key", hk, "Enter switches to the next key (" + ", ".join(h["label"] for h in HOTKEYS) + ")",
             self.next_hotkey),
            ("Check for updates", p.get("auto_update", True), "when valset starts, at most every 6 hours",
             lambda: self.set_pref("auto_update", not p.get("auto_update", True))),
            ("Install update" if self.update else "Check now", upd, "", self.update_action),
        ]

    def next_hotkey(self):
        labels = [h["label"] for h in HOTKEYS]
        cur = labels.index(self.prefs["hotkey"]["label"]) if self.prefs["hotkey"]["label"] in labels else -1
        self.set_pref("hotkey", HOTKEYS[(cur + 1) % len(HOTKEYS)])
        if not self.hotkey.error:
            self.toast(f"Dodge hotkey is now {self.prefs['hotkey']['label']}.", "ok")

    def options_lines(self, w):
        rows = self.option_rows()
        sel = self.sel["options"] = max(0, min(self.sel["options"], len(rows) - 1))
        out = ["", " " + paint("Options", C["text"], True), ""]
        for i, (label, value, desc, _) in enumerate(rows):
            if value is True or value is False:
                val = paint(" ON  ", "#04130b", True, C["good"]) if value else paint(" OFF ", C["muted"], True, C["line"])
            elif isinstance(value, str) and ANSI.search(value):
                val = " " + value
            else:
                val = paint(f" {value} ", C["text"], True, C["line"])
            val += " " * max(0, 16 - vlen(val))  # line the descriptions up
            text = paint(fit(label, 20), C["text"], True) + val + "  " + paint(desc, C["muted"])
            if i == sel:
                self.focus_line = len(out)
            out.append(self.row_line(i == sel, text, w))
            if label == "Hotkey key" and self.hotkey.error:
                out.append("      " + paint(self.hotkey.error, C["bad"], True))
        if self.update and self.update_state == "idle":
            out += ["", " " + paint(f"What's new in {self.update['tag']}", C["muted"], True)]
            out += ["   " + paint(fit("· " + n, w - 6), C["muted"]) for n in updater.short_notes(self.update, 5)]
        out += ["", " " + paint("Text too small? ", C["muted"]) + paint("Ctrl + mouse wheel", C["text"], True) +
                paint(" makes it bigger.", C["muted"]),
                " " + paint("Second monitor: drag this window there. The board fits narrow windows too.", C["muted"]),
                " " + paint(f"valset v{VERSION}", C["muted"])]
        return out

    # ---- keys

    def on_key(self, ch):
        if self.dialog:
            self.dialog["on_key"](ch)
            return True
        c = ch.lower() if isinstance(ch, str) and len(ch) == 1 else ch
        if c in ("q", "\x03"):
            return False
        tabs = [t for t, _ in TABS]
        if c in ("1", "2", "3"):
            return self.go_tab(tabs[int(c) - 1])
        if c in ("\t", "right", "left"):
            return self.go_tab(tabs[(tabs.index(self.tab) + (-1 if c == "left" else 1)) % len(tabs)])
        if self.tab == "live":
            if c in ("up", "down", "pgup", "pgdn", "home", "end"):
                h = self.con.dims()[1] - 6
                delta = {"up": -1, "down": 1, "pgup": -h, "pgdn": h, "home": -10 ** 6, "end": 10 ** 6}[c]
                self.scroll["live"] = max(0, self.scroll["live"] + delta)
            elif c == "d":
                b = self.live.board
                if not b or not b["can_dodge"]:
                    self.toast("You can only dodge during agent select.", "warn")
                else:
                    self.confirm("Dodge this match?", lambda: self.bg(self.live.dodge, None),
                                 detail="You lose RR in competitive and get a queue cooldown.", yes="Y", danger=True)
        elif self.tab == "saves":
            self.saves_key(c)
        elif self.tab == "options":
            rows = self.option_rows()
            if c in ("up", "down", "home", "end"):
                step = {"up": -1, "down": 1, "home": -99, "end": 99}[c]
                self.sel["options"] = max(0, min(len(rows) - 1, self.sel["options"] + step))
            elif c in ("\r", "\n", " "):
                rows[self.sel["options"]][3]()
        return True

    def go_tab(self, tab):
        self.tab = tab
        if tab == "saves":
            self.load_saves()  # pick up saves made elsewhere and the current account
        return True

    def line_input(self, prompt):
        """Read a line on the bottom row. Returns the text, or None if Esc was pressed."""
        import msvcrt
        buf = ""
        sys.stdout.write("\x1b[?25h")
        try:
            while True:
                w, h = self.con.dims()
                sys.stdout.write(f"\x1b[{h};1H\x1b[K" + clip(" " + paint(prompt, C["warn"], True) + buf, w - 1))
                sys.stdout.flush()
                ch = msvcrt.getwch()
                if ch in ("\r", "\n"):
                    return buf.strip()
                if ch == "\x1b":
                    return None
                if ch in ("\x00", "\xe0"):
                    msvcrt.getwch()
                elif ch == "\x08":
                    buf = buf[:-1]
                elif ch.isprintable():
                    buf += ch
        finally:
            sys.stdout.write("\x1b[?25l")
            self.con.prev = []  # repaint everything after the prompt

    # ---- main loop

    def run(self):
        if os.name != "nt" or not sys.stdin.isatty():
            raise AppError("The valset screen needs a real Windows console. Try: valset live")
        self.con = Console()
        seen, last_frame = None, None
        try:
            while True:
                while True:
                    try:
                        cb, result, err = self.tasks.get_nowait()
                    except queue.Empty:
                        break
                    if err:
                        (cb or (lambda m: self.toast(m, "error")))(err)
                    elif cb:
                        cb(result)
                for kind, text in self.live.take_events():
                    self.toast(text, kind)
                state = (self.live.version, self.tab, self.scroll[self.tab], self.msg if time.time() < self.msg_until else "",
                         id(self.dialog), self.dialog_v, self.busy, tuple(self.sel.values()), id(self.saves),
                         self.con.dims(), tuple(sorted((k, str(v)) for k, v in self.prefs.items())), self.hotkey.error,
                         self.update_state, round(self.update_pct, 2), id(self.update))
                if state != seen:
                    seen = state
                    frame = self.frame()
                    if frame != last_frame:
                        self.con.draw(frame)
                        last_frame = frame
                if self.restart_after:
                    break
                key = self.con.wait_key(0.3)
                if key is not None and not self.on_key(key):
                    break
        except KeyboardInterrupt:
            pass
        finally:
            self.con.close()
            self.hotkey.stop()
        if self.restart_after:
            print(f"{self.update_msg} Starting it...")
            if not updater.restart():
                print("Open valset again to use the new version.")


def run():
    TUI().run()
