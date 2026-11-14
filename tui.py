"""valset in the terminal: a full-screen, keyboard-driven view of the live match, your
matches, settings saves and options.

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
import tracker
import updater
from valset import CATEGORIES, VERSION, AppError

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
TABS = [("live", "Live"), ("history", "My matches"), ("saves", "Saves"), ("options", "Options")]
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
                return {"H": "up", "P": "down", "I": "pgup", "Q": "pgdn", "G": "home", "O": "end"}.get(msvcrt.getwch())
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
        self.msg, self.msg_until, self.msg_kind = "", 0, "info"
        self.history = None
        self.history_loading = False
        self.saves = None
        self.tasks = queue.Queue()
        self.ask = None  # (question, on_key) while waiting for a yes/no key
        self.con = None
        self.update = None  # newer GitHub release, if there is one
        self.update_state, self.update_msg, self.update_pct = "idle", "", 0.0
        self.restart_after = False
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

    # ---- updates

    def check_update(self, force=True, quiet=False):
        self.update_state, self.update_msg = "checking", ""

        def done(rel):
            self.update, self.update_state = rel, "idle"
            if rel:
                self.toast(f"valset {rel['tag']} is out. Press 4 then U to update.", "info")
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

    def update_key(self):
        if self.update_state in ("checking", "downloading"):
            return
        if self.update_state == "done":
            return self.toast(self.update_msg, "ok")
        if not self.update:
            self.toast("Checking GitHub for updates...", "info")
            return self.check_update(force=True)
        if updater.install_kind() == "git":
            return self.toast("This folder is a git checkout, so valset won't overwrite it. Run: git pull", "warn")
        restart = " valset restarts by itself." if updater.install_kind() == "exe" else ""
        self.confirm(f"Install valset {self.update['tag']}?{restart}  Y = update, any other key = not now",
                     self.install_update)

    def hk_label(self):
        return self.prefs["hotkey"]["label"] if self.prefs.get("hotkey_enabled") else None

    # ---- frame

    def frame(self):
        w, h = self.con.dims()
        st, b = self.live.status, self.live.board
        top = []
        acc = paint("● " + st["account"], C["good"]) if st["riot"] == "ok" else \
            paint("● connecting...", C["muted"]) if st["riot"] == "starting" else paint("● Riot Client not connected", C["bad"])
        game = paint("● VALORANT " + ("open" if st["game_open"] else "closed"), C["good"] if st["game_open"] else C["muted"])
        phase = ("   " + paint("● " + ("Agent select" if b["phase"] == "agent select" else "In a match"), C["warn"], True)) if b else ""
        upd = ("   " + paint(f"⬆ {self.update['tag']} out · 4 › U", C["info"], True)) \
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

        body = {"live": self.live_lines, "history": self.history_lines, "saves": self.saves_lines,
                "options": self.options_lines}[self.tab](w)

        foot = [paint("─" * (w - 1), C["line"])]
        if self.ask:
            foot.append(" " + paint(self.ask[0], C["warn"], True))
        elif self.msg and time.time() < self.msg_until:
            col = {"ok": C["good"], "error": C["bad"], "warn": C["warn"]}.get(self.msg_kind, C["info"])
            foot.append(" " + paint(self.msg, col, True))
        else:
            foot.append(" " + self.keys_hint())
        room = h - len(top) - len(foot)
        max_scroll = max(0, len(body) - room)
        self.scroll[self.tab] = min(self.scroll[self.tab], max_scroll)
        off = self.scroll[self.tab]
        view = body[off:off + room]
        if max_scroll:
            more = f" ↑↓ scroll  {off + 1}-{off + len(view)} of {len(body)} "
            foot[0] = paint("─" * (w - 2 - len(more)), C["line"]) + paint(more, C["muted"])
        return top + view + [""] * (room - len(view)) + foot

    def keys_hint(self):
        k = lambda key, label: paint(f"[{key}]", C["accent"], True) + paint(f" {label}  ", C["muted"])
        common = k("1-4", "section") + k("Q", "quit")
        if self.tab == "live":
            b = self.live.board
            return (k("D", "dodge") if b and b["can_dodge"] else "") + common
        if self.tab == "history":
            return k("R", "refresh") + common
        if self.tab == "saves":
            return k("S", "save") + k("L", "load") + k("U", "undo") + k("X", "delete") + common
        return (k("H", "hotkey on/off") + k("K", "change hotkey") + k("S", "skins") + k("C", "compact")
                + k("U", "update" if self.update else "check updates") + k("A", "auto-check") + common)

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

    # ---- my matches

    def load_history(self):
        if self.history_loading:
            return
        self.history_loading = True

        def done(d):
            self.history, self.history_loading = d, False

        def fail(m):
            self.history, self.history_loading = {"error": m}, False
        self.bg(lambda: tracker.history_data(self.live.get_api(), 15), done, fail)

    def history_lines(self, w):
        d = self.history
        if self.history_loading and not d:
            return ["", "   " + paint("Loading your matches...", C["muted"])]
        if not d:
            return ["", "   " + paint("Press R to load your matches.", C["muted"])]
        if d.get("error"):
            return ["", "   " + paint("Couldn't load your matches", C["text"], True), "   " + paint(d["error"], C["muted"])]
        ms = d["matches"]
        wins, losses = sum(m["result"] == "win" for m in ms), sum(m["result"] == "loss" for m in ms)
        k, dth = sum(m["k"] for m in ms), sum(m["d"] for m in ms)
        hs = [m["hs"] for m in ms if m["hs"] is not None]
        rk, pk = d["rank"], d["peak"]
        out = ["", " " + paint(d["account"], C["text"], True) + (paint("   (refreshing...)", C["muted"]) if self.history_loading else ""), ""]
        out.append(" " + (paint(f"{rk['name']} {rk['rr']}rr" if rk["tier"] > 2 else "Unranked", rank_col(rk["tier"]), True) if rk else "?") +
                   paint("   peak ", C["muted"]) + (paint(pk["name"], rank_col(pk["tier"]), True) if pk else "-") +
                   paint("   win % this act ", C["muted"]) + paint(f"{d['wr']}%" if d["wr"] is not None else "-", C["text"], True) +
                   paint(f" ({d['games']} games)", C["muted"]))
        out.append(" " + paint(f"last {len(ms)} ", C["muted"]) + paint(f"{wins}W {losses}L", C["text"], True) +
                   paint("   K/D ", C["muted"]) + paint(f"{k / max(dth, 1):.2f}", C["text"], True) +
                   paint("   HS ", C["muted"]) + paint(f"{round(sum(hs) / len(hs))}%" if hs else "-", C["text"], True))
        out += ["", paint(f"   {fit('RESULT', 13)}{fit('MAP', 10)}{fit('MODE', 9)}{fit('AGENT', 10)}{fit('K / D / A', 12)}"
                          f"{'ACS':>4} {'HS%':>4} {'RR':>4}  WHEN", C["muted"])]
        for m in ms:
            col = {"win": C["good"], "loss": C["bad"]}.get(m["result"], C["warn"])
            res = {"win": "WIN", "loss": "LOSS"}.get(m["result"], "DRAW") + (f" {m['score']}" if m["score"] else "")
            rr = m["rr"]
            rr_s = paint(f"{('+' if rr > 0 else '') + str(rr):>4}", C["good"] if rr > 0 else C["bad"] if rr < 0 else C["muted"], True) \
                if rr is not None else paint(f"{'-':>4}", C["muted"])
            out.append(f" {paint('▌', col)} " + paint(fit(res, 13), col, True) + fit(m["map"], 10) +
                       paint(fit(m["queue_name"], 9), C["muted"]) + fit(m["agent"], 10) +
                       paint(fit(f"{m['k']} / {m['d']} / {m['a']}", 12), C["text"], True) +
                       f"{m['acs'] if m['acs'] is not None else '-':>4} {(str(m['hs']) + '%') if m['hs'] is not None else '-':>4} "
                       f"{rr_s}  " + paint(ago(m["start"]), C["muted"]))
        return out

    # ---- saves

    def load_saves(self):
        def done(d):
            self.saves = d
        self.bg(app.saves_data, done)

    def save_list(self):
        d = self.saves or {"saves": []}
        mine = [x for x in d["saves"] if x["mine"] and not x["auto"]]
        other = [x for x in d["saves"] if not x["mine"] and not x["auto"]]
        auto = [x for x in d["saves"] if x["auto"]]
        return mine, other, auto

    def saves_lines(self, w):
        d = self.saves
        if not d:
            return ["", "   " + paint("Loading your saves...", C["muted"])]
        out = ["", " " + paint("Settings saves", C["text"], True) + paint(
            f"   for {d['account']}" if d["account"] else "   (sign in to the Riot Client to save or load)", C["muted"])]
        if self.live.status.get("game_open"):
            out += ["", " " + paint("Close VALORANT before loading a save. The game overwrites your settings when it exits.",
                                    C["warn"], True)]
        n = 0
        for title, items in zip(("THIS ACCOUNT", "OTHER ACCOUNTS", "AUTOMATIC BACKUPS"), self.save_list()):
            if not items and title != "THIS ACCOUNT":
                continue
            out += ["", " " + paint(title, C["muted"], True)]
            if not items:
                out.append("   " + paint("No saves for this account yet. Press S to save.", C["muted"]))
            for x in items:
                n += 1
                when = ""
                try:
                    when = datetime.fromisoformat(x["created"]).strftime("%b %d %H:%M")
                except (TypeError, ValueError):
                    pass
                out.append(f"  {paint(f'{n:>2}', C['accent'], True)}  " + paint(fit(x["label"], 28), C["text"], True) +
                           paint(fit(f"from {x['account']}", 30) + when + ("  + video" if x["has_video"] else ""), C["muted"]))
        out += ["", " " + paint("S", C["accent"], True) + paint(" save my settings   ", C["muted"]) +
                paint("L", C["accent"], True) + paint(" load a save onto this account   ", C["muted"]) +
                paint("U", C["accent"], True) + paint(" undo the last load" + ("" if d["can_undo"] else " (nothing to undo)") + "   ", C["muted"]) +
                paint("X", C["accent"], True) + paint(" delete", C["muted"])]
        return out

    def numbered_saves(self):
        mine, other, auto = self.save_list()
        return mine + other + auto

    # ---- options

    def options_lines(self, w):
        p = self.prefs
        on = lambda v: paint(" ON  ", "#04130b", True, C["good"]) if v else paint(" OFF ", C["muted"], True, C["head"])
        k = lambda key: paint(f"[{key}]", C["accent"], True)
        out = ["", " " + paint("Options", C["text"], True), ""]
        out.append(f"  {k('H')} Dodge hotkey        {on(p.get('hotkey_enabled'))}   " + paint(p["hotkey"]["label"], C["text"], True))
        out.append("      " + paint("Works from inside the game. Press it twice within 3 seconds in agent select to dodge.", C["muted"]))
        out.append("      " + paint("The first press beeps, so one slip can't dodge you.", C["muted"]))
        if self.hotkey.error:
            out.append("      " + paint(self.hotkey.error, C["bad"], True))
        out.append(f"  {k('K')} Change hotkey       " + paint(" → ".join(h["label"] for h in HOTKEYS), C["muted"]))
        out.append("")
        out.append(f"  {k('S')} Show skins          {on(p.get('show_skins'))}   " + paint("Vandal, Phantom and knife under each player", C["muted"]))
        out.append(f"  {k('C')} Compact             {on(p.get('compact'))}   " + paint("one line per player, no skins", C["muted"]))
        out += ["", " " + paint("Updates", C["text"], True), ""]
        where = {"exe": "valset.exe", "git": "git checkout", "source": "Python source"}[updater.install_kind()]
        out.append("      This copy is " + paint(f"v{VERSION}", C["text"], True) + paint(f"  ({where})", C["muted"]))
        if self.update_state == "checking":
            line = paint("Checking GitHub...", C["muted"])
        elif self.update_state == "downloading":
            n = int(self.update_pct * 30)
            line = paint(f"Downloading {self.update['tag']}  ", C["info"], True) + paint("█" * n, C["info"]) + \
                paint("░" * (30 - n), C["line"]) + paint(f" {self.update_pct * 100:3.0f}%", C["muted"])
        elif self.update_state == "done":
            line = paint(self.update_msg, C["good"], True)
        elif self.update:
            line = paint(f"{self.update['tag']} is out", C["info"], True) + paint("  press U to install it", C["muted"])
        else:
            line = paint(self.update_msg or "Not checked yet.", C["muted"])
        out.append(f"  {k('U')} {'Update now' if self.update else 'Check now':<20} " + line)
        if self.update and self.update_state == "idle":
            for note in updater.short_notes(self.update, 5):
                out.append("        " + paint(fit("· " + note, w - 10), C["muted"]))
        out.append(f"  {k('A')} Check on start      {on(p.get('auto_update', True))}   " +
                   paint("asks GitHub at most every 6 hours, never installs until you press U", C["muted"]))
        out += ["", " " + paint("Text size: ", C["muted"]) + paint("Ctrl + mouse wheel", C["text"], True) +
                paint(" (or Ctrl + / Ctrl -) in the terminal.", C["muted"]),
                " " + paint("Second monitor: drag this terminal there. The board fits narrow (portrait) windows too.", C["muted"])]
        return out

    # ---- keys

    def on_key(self, ch):
        if self.ask:
            q, fn = self.ask
            self.ask = None
            fn(ch)
            return True
        c = ch.lower() if isinstance(ch, str) and len(ch) == 1 else ch
        if c in ("q", "\x1b", "\x03"):
            return False
        if c in ("1", "2", "3", "4"):
            self.tab = TABS[int(c) - 1][0]
            if self.tab == "history" and not self.history:
                self.load_history()
            if self.tab == "saves":
                self.load_saves()
            return True
        if c in ("up", "down", "pgup", "pgdn", "home", "end"):
            h = self.con.dims()[1] - 6
            delta = {"up": -1, "down": 1, "pgup": -h, "pgdn": h, "home": -10 ** 6, "end": 10 ** 6}[c]
            self.scroll[self.tab] = max(0, self.scroll[self.tab] + delta)
            return True
        if self.tab == "live" and c == "d":
            b = self.live.board
            if not b or not b["can_dodge"]:
                self.toast("You can only dodge during agent select.", "warn")
            else:
                self.confirm("Dodge? You lose RR in competitive and get a queue cooldown.  Y = dodge, any other key = stay",
                             lambda: self.bg(self.live.dodge, None))
        elif self.tab == "history" and c == "r":
            self.load_history()
        elif self.tab == "saves":
            self.saves_key(c)
        elif self.tab == "options":
            if c == "h":
                self.set_pref("hotkey_enabled", not self.prefs.get("hotkey_enabled"))
            elif c == "k":
                labels = [h["label"] for h in HOTKEYS]
                cur = labels.index(self.prefs["hotkey"]["label"]) if self.prefs["hotkey"]["label"] in labels else -1
                self.set_pref("hotkey", HOTKEYS[(cur + 1) % len(HOTKEYS)])
                if not self.hotkey.error:
                    self.toast(f"Dodge hotkey is now {self.prefs['hotkey']['label']}.", "ok")
            elif c == "s":
                self.set_pref("show_skins", not self.prefs.get("show_skins"))
            elif c == "c":
                self.set_pref("compact", not self.prefs.get("compact"))
            elif c == "u":
                self.update_key()
            elif c == "a":
                self.set_pref("auto_update", not self.prefs.get("auto_update", True))
        return True

    def confirm(self, question, fn):
        self.ask = (question, lambda ch: fn() if isinstance(ch, str) and ch.lower() == "y" else self.toast("Cancelled.", "info"))

    def saves_key(self, c):
        d = self.saves
        if not d:
            return
        done = lambda msg: (self.toast(msg, "ok"), self.load_saves())
        if c == "s":
            if not d["account"]:
                return self.toast("Sign in to the Riot Client first.", "warn")
            label = self.line_input(f"Name for this save (Enter = \"{d['account']}\", Esc = cancel): ")
            if label is not None:
                self.toast("Saving...", "info")
                self.bg(lambda: app.do_save(label), done)
        elif c == "u":
            if not d["can_undo"]:
                return self.toast("Nothing to undo on this account.", "info")
            self.confirm(f"Undo the last load on {d['account']}?  Y = undo, any other key = cancel",
                         lambda: self.bg(app.do_undo, done))
        elif c in ("l", "x"):
            items = self.numbered_saves()
            if not items:
                return self.toast("No saves yet.", "info")
            n = self.line_input(f"{'Load' if c == 'l' else 'Delete'} which save? Type its number (Esc = cancel): ")
            if not n or not n.isdigit() or not 1 <= int(n) <= len(items):
                return self.toast("Cancelled." if n is None else "That number isn't in the list.", "info")
            x = items[int(n) - 1]
            if c == "x":
                return self.confirm(f"Delete \"{x['label']}\" for good?  Y = delete, any other key = keep",
                                    lambda: self.bg(lambda: app.do_delete(x["id"]), done))
            if self.live.status.get("game_open"):
                return self.toast("Close VALORANT first. The game overwrites your settings when it exits.", "warn")
            parts = self.line_input("Load which parts? Enter = everything, or type some of: " + " ".join(CATEGORIES) + "  : ")
            if parts is None:
                return self.toast("Cancelled.", "info")
            chosen = [p for p in parts.lower().split() if p in CATEGORIES] or list(CATEGORIES)
            self.confirm(f"Load \"{x['label']}\" ({', '.join(chosen)}) onto {d['account']}? Current settings are backed up first.  "
                         f"Y = load", lambda: (self.toast("Loading...", "info"),
                                              self.bg(lambda: app.do_load(x["id"], chosen), done)))

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
                         self.ask, self.history_loading, id(self.history), id(self.saves), self.con.dims(),
                         tuple(sorted((k, str(v)) for k, v in self.prefs.items())), self.hotkey.error,
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


def ago(ms):
    if not ms:
        return ""
    m = round((time.time() * 1000 - ms) / 60000)
    if m < 60:
        return f"{m} min ago"
    h = round(m / 60)
    return f"{h} h ago" if h < 24 else f"{round(h / 24)} d ago"


def run():
    TUI().run()
