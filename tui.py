"""valset in the terminal: a full-screen, keyboard- and mouse-driven view of the live match,
your session, the players you've met, your settings saves and options. Built to sit on a
second monitor: it fills in by itself and every row can be clicked.

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
from collections import deque
from ctypes import wintypes
from datetime import datetime, timedelta

import app
import people
import tracker
import updater
from valset import VERSION, AppError

# ---------------------------------------------------------------------------- colours

C = dict(text="#ece8e1", muted="#7f8d9b", dim="#4d5c6a", accent="#ff4655", good="#3ddc97", bad="#ff5c6c",
         warn="#ffc857", info="#5cc8ff", gold="#f5d26b", line="#24384a", ally="#3ddcb4", enemy="#ff5c6c",
         ink="#0b1218")
BG = dict(page="#0f1923", panel="#16232f", stripe="#121f2a", sel="#1e3345", chip="#25394b")
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
TABS = [("live", "Live"), ("players", "Players"), ("saves", "Saves"), ("options", "Options")]
RESET, BOLD, ITALIC = "\x1b[0m", "\x1b[1m", "\x1b[3m"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def fg(hexcol):
    h = hexcol.lstrip("#")
    return f"\x1b[38;2;{int(h[0:2], 16)};{int(h[2:4], 16)};{int(h[4:6], 16)}m"


def bgc(hexcol):
    h = hexcol.lstrip("#")
    return f"\x1b[48;2;{int(h[0:2], 16)};{int(h[2:4], 16)};{int(h[4:6], 16)}m"


def paint(text, color=None, bold=False, bg=None, italic=False):
    return (BOLD if bold else "") + (ITALIC if italic else "") + (fg(color) if color else "") + \
        (bgc(bg) if bg else "") + str(text) + RESET


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


def band(text, w, bg):
    """A full-width line on its own background (header, footer, highlighted rows)."""
    b = bgc(bg)
    return b + text.replace(RESET, RESET + b) + " " * max(0, w - vlen(text)) + RESET


def rank_col(tier):
    return RANK_COLORS[min(8, (tier - 3) // 3)] if tier and tier >= 3 else C["muted"]


def chip(text, color=C["text"], bg=BG["chip"]):
    return paint(f" {text} ", color, True, bg)


def bar(frac, width, on, off=C["line"], off_char="░"):
    n = max(0, min(width, round(frac * width)))
    return paint("█" * n, on) + paint(off_char * (width - n), off)


def kda(g):
    return f"{g['k']}/{g['d']}/{g['a']}"


def when(ms):
    """'12m ago', '3h ago', 'yesterday 21:04', 'Nov 20'."""
    if not ms:
        return ""
    dt, now = datetime.fromtimestamp(ms / 1000), datetime.now()
    mins = (now - dt).total_seconds() / 60
    if mins < 60:
        return f"{max(1, int(mins))}m ago"
    if mins < 12 * 60:
        return f"{int(mins // 60)}h ago"
    if dt.date() == now.date():
        return dt.strftime("today %H:%M")
    if dt.date() == (now - timedelta(days=1)).date():
        return dt.strftime("yesterday %H:%M")
    if (now - dt).days < 7:
        return dt.strftime("%a %H:%M")
    return dt.strftime("%b %d")


class Line(str):
    """A screen line that also knows which parts of it can be clicked: spans of
    (x0, x1, action) where action is a key (str) or a callable(double_click)."""
    def __new__(cls, text, spans=()):
        obj = super().__new__(cls, text)
        obj.spans = list(spans)
        return obj


def keys_line(items, lead=" ", gap="   "):
    """[(key, label, action, style)] -> a Line of key chips you can click."""
    text, spans, x = lead, [], vlen(lead)
    for key, label, action, *style in items:
        style = style[0] if style else ""
        bgk = {"primary": C["info"], "danger": C["accent"], "good": C["good"]}.get(style)
        k = paint(f" {key} ", C["ink"] if bgk else C["text"], True, bgk or BG["chip"])
        t = k + paint(f" {label}", C["muted"]) + gap
        spans.append((x, x + vlen(k) + len(label) + 1, action))
        x += vlen(t)
        text += t
    return Line(text, spans)


# ---------------------------------------------------------------------------- console

class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD), ("wVirtualKeyCode", wintypes.WORD),
                ("wVirtualScanCode", wintypes.WORD), ("uChar", wintypes.WCHAR), ("dwControlKeyState", wintypes.DWORD)]


class MOUSE_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT), ("dwButtonState", wintypes.DWORD),
                ("dwControlKeyState", wintypes.DWORD), ("dwEventFlags", wintypes.DWORD)]


class _EVENT(ctypes.Union):
    _fields_ = [("Key", KEY_EVENT_RECORD), ("Mouse", MOUSE_EVENT_RECORD), ("pad", ctypes.c_byte * 16)]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD), ("Event", _EVENT)]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [("Left", wintypes.SHORT), ("Top", wintypes.SHORT), ("Right", wintypes.SHORT), ("Bottom", wintypes.SHORT)]


class CSBI(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("dwCursorPosition", wintypes.DWORD), ("wAttributes", wintypes.WORD),
                ("srWindow", SMALL_RECT), ("dwMaximumWindowSize", wintypes.DWORD)]


VK_NAMES = {0x26: "up", 0x28: "down", 0x25: "left", 0x27: "right", 0x21: "pgup", 0x22: "pgdn",
            0x24: "home", 0x23: "end", 0x2E: "del", 0x70: "f1"}


class Console:
    STD_INPUT, STD_OUTPUT = -10, -11
    MOUSE_INPUT, QUICK_EDIT, EXTENDED, WINDOW_INPUT, VT_INPUT = 0x10, 0x40, 0x80, 0x8, 0x200

    def __init__(self, mouse=True, themed=True):
        self.prev = []
        self.size = (0, 0)
        self.page = BG["page"] if themed else None
        self.pending = deque()
        self.btn = 0
        self.k32 = ctypes.windll.kernel32
        self.hin = self.k32.GetStdHandle(self.STD_INPUT)
        self.hout = self.k32.GetStdHandle(self.STD_OUTPUT)
        self.k32.SetConsoleTitleW("valset")
        self.mode0 = wintypes.DWORD()
        self.has_mode = bool(self.k32.GetConsoleMode(self.hin, ctypes.byref(self.mode0)))
        self.set_mouse(mouse)
        sys.stdout.write("\x1b[?1049h\x1b[?25l")  # own screen, hide cursor
        self.clear()

    def set_mouse(self, on):
        if not self.has_mode:
            return
        m = (self.mode0.value | self.EXTENDED | self.WINDOW_INPUT) & ~self.VT_INPUT
        m = (m | self.MOUSE_INPUT) & ~self.QUICK_EDIT if on else (m & ~self.MOUSE_INPUT)
        self.k32.SetConsoleMode(self.hin, m)

    def set_theme(self, themed):
        self.page = BG["page"] if themed else None
        self.clear()

    def clear(self):
        sys.stdout.write("\x1b[0m" + (bgc(self.page) if self.page else "") + "\x1b[2J")
        sys.stdout.flush()
        self.prev = []

    def close(self):
        if self.has_mode:
            self.k32.SetConsoleMode(self.hin, self.mode0.value)
        sys.stdout.write("\x1b[0m\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()

    def title(self, text):
        self.k32.SetConsoleTitleW(text)

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
            self.size = (w, h)
            self.clear()
        page = bgc(self.page) if self.page else ""
        lines = [page + clip(line, w - 1).replace(RESET, RESET + page) for line in lines[:h]]
        out = []
        for i in range(max(len(lines), len(self.prev))):
            new = lines[i] if i < len(lines) else page
            if i >= len(self.prev) or self.prev[i] != new:
                out.append(f"\x1b[{i + 1};1H{new}\x1b[K")
        if out:
            sys.stdout.write("".join(out) + RESET)
            sys.stdout.flush()
        self.prev = lines

    def _top(self):
        info = CSBI()
        if self.k32.GetConsoleScreenBufferInfo(self.hout, ctypes.byref(info)):
            return info.srWindow.Top
        return 0

    def _decode(self, rec):
        """A key name/character, a mouse dict, or None for events we don't use."""
        if rec.EventType == 1:
            k = rec.Event.Key
            if not k.bKeyDown:
                return None
            if k.wVirtualKeyCode in VK_NAMES:
                return VK_NAMES[k.wVirtualKeyCode]
            if k.wVirtualKeyCode == 0x09 and k.dwControlKeyState & 0x10:
                return "btab"
            return k.uChar if k.uChar and k.uChar != "\x00" else None
        if rec.EventType == 2:
            m = rec.Event.Mouse
            y = m.Y - self._top()
            if m.dwEventFlags & 0x4:  # wheel: the high word is the signed distance
                return {"mouse": "wheel", "x": m.X, "y": y, "up": ctypes.c_short(m.dwButtonState >> 16).value > 0}
            pressed = m.dwButtonState & 1
            was, self.btn = self.btn, pressed
            if m.dwEventFlags & 0x2 and pressed:
                return {"mouse": "dclick", "x": m.X, "y": y}
            if m.dwEventFlags == 0 and pressed and not was:
                return {"mouse": "click", "x": m.X, "y": y}
        return None

    def wait_key(self, timeout):
        """Sleep until a key/click arrives or `timeout` seconds pass. Returns it or None."""
        if self.pending:
            return self.pending.popleft()
        n = wintypes.DWORD()
        self.k32.GetNumberOfConsoleInputEvents(self.hin, ctypes.byref(n))
        if not n.value:
            self.k32.WaitForSingleObject(self.hin, int(timeout * 1000))
            self.k32.GetNumberOfConsoleInputEvents(self.hin, ctypes.byref(n))
        if not n.value:
            return None
        recs = (INPUT_RECORD * min(64, n.value))()
        got = wintypes.DWORD()
        if not self.k32.ReadConsoleInputW(self.hin, recs, len(recs), ctypes.byref(got)):
            return None
        for i in range(got.value):
            ev = self._decode(recs[i])
            if ev is not None:
                self.pending.append(ev)
        return self.pending.popleft() if self.pending else None


# ---------------------------------------------------------------------------- the app

class TUI:
    def __init__(self):
        self.prefs = app.load_prefs()
        self.live = app.Live()
        self.hotkey = app.Hotkey(self.live.hotkey_pressed)
        self.apply_hotkey()
        self.tab = "live"
        self.scroll = {t: 0 for t, _ in TABS}
        self.sel = {"saves": 0, "options": 0, "players": 0}  # highlighted row on the list screens
        self.pick = None  # puuid of the highlighted player on the live board
        self.focus_line = None  # body line of the highlighted row, so scrolling follows it
        self.hits = []  # (y, x0, x1, action) for mouse clicks, rebuilt every frame
        self.msg, self.msg_until, self.msg_kind = "", 0, "info"
        self.saves = None
        self.people, self.people_v, self.filter = [], 0, ""
        self.busy = ""  # "Saving...", "Loading..." while a save action runs
        self.tasks = queue.Queue()
        self.dialog = None  # {"lines": fn() -> [str], "on_key": fn(key)} while a question is open
        self.dialog_v = 0
        self.con = None
        self.update = None  # newer GitHub release, if there is one
        self.update_state, self.update_msg, self.update_pct = "idle", "", 0.0
        self.restart_after = False
        self.title_text = ""
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
        if key == "theme_bg" and self.con:
            self.con.set_theme(value)
        if key == "mouse" and self.con:
            self.con.set_mouse(value)

    def toast(self, text, kind="info"):
        self.msg, self.msg_kind, self.msg_until = text, kind, time.time() + (9 if kind in ("error", "avoid") else 6)

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

    # ---- dialogs: a short question in the footer, answered with one key (or a click)

    def open_dialog(self, lines, on_key):
        self.dialog = {"lines": lines, "on_key": on_key}
        self.dialog_v += 1

    def close_dialog(self):
        self.dialog = None
        self.dialog_v += 1

    def confirm(self, question, fn, detail="", yes="Enter", danger=False):
        """Yes/no question. `yes` is the key that means yes ("Enter" or "Y"); Esc or any other key cancels."""
        keys = ("\r", "\n") if yes == "Enter" else ("y", "Y")

        def lines():
            out = [" " + paint(question, C["text"], True)]
            if detail:
                out.append(" " + paint(detail, C["muted"]))
            out.append(keys_line([(yes, "yes", keys[0], "danger" if danger else "primary"), ("Esc", "no", "\x1b")]))
            return out

        def on_key(ch):
            self.close_dialog()
            if ch in keys:
                fn()
            else:
                self.toast("Cancelled.", "info")
        self.open_dialog(lines, on_key)

    def ask_text(self, question, done, initial="", hint=""):
        """Type a line in the footer. Enter = done(text), Esc = cancel. The screen keeps updating."""
        buf = [initial]

        def lines():
            cursor = paint("_", C["warn"], True)
            out = [" " + paint(question, C["text"], True) + ("   " + paint(hint, C["muted"]) if hint else ""),
                   " " + paint(" > ", C["ink"], True, C["warn"]) + " " + paint(buf[0], C["text"], True) + cursor,
                   keys_line([("Enter", "done", "\r", "primary"), ("Esc", "cancel", "\x1b")])]
            return out

        def on_key(ch):
            if not isinstance(ch, str):
                return
            if ch in ("\r", "\n"):
                self.close_dialog()
                done(buf[0].strip())
            elif ch == "\x1b":
                self.close_dialog()
                self.toast("Cancelled.", "info")
            elif ch == "\x08":
                buf[0] = buf[0][:-1]
            elif len(ch) == 1 and ch.isprintable() and len(buf[0]) < 120:
                buf[0] += ch
            self.dialog_v += 1
        self.open_dialog(lines, on_key)

    # ---- updates

    def check_update(self, force=True, quiet=False):
        self.update_state, self.update_msg = "checking", ""

        def done(rel):
            self.update, self.update_state = rel, "idle"
            if rel:
                self.toast(f"valset {rel['tag']} is out. Go to Options (4) to install it.", "info")
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

    def header(self, w):
        st, b, ses = self.live.status, self.live.board, self.live.session
        logo = paint(" ◆ ", C["accent"], True) + paint("val", C["text"], True) + paint("set", C["accent"], True)
        if st["riot"] == "ok":
            acc = paint("●", C["good"]) + " " + paint(st["account"], C["text"], True)
        elif st["riot"] == "starting":
            acc = paint("● connecting...", C["muted"])
        else:
            acc = paint("● Riot Client not connected", C["bad"], True)
        game = paint("● VALORANT", C["good"] if st["game_open"] else C["dim"]) + \
            paint(" open" if st["game_open"] else " closed", C["muted"])
        left = logo + "   " + acc + "   " + game
        if b:
            sel = b["phase"] == "agent select"
            left += "   " + chip("AGENT SELECT" if sel else "IN GAME", C["ink"], C["warn"] if sel else C["good"])
        if self.update and self.update_state != "done":
            left += "   " + paint(f"↑ {self.update['tag']} out", C["info"], True)
        right = ""
        if ses and ses["games"]:
            right = paint("Tonight ", C["muted"]) + paint(f"{ses['w']}W", C["good"], True) + " " + \
                paint(f"{ses['l']}L", C["bad"], True)
            if ses["rr"] is not None:
                right += "  " + paint(f"{ses['rr']:+d} RR", C["good"] if ses["rr"] >= 0 else C["bad"], True)
            right += "   "
        right += paint(datetime.now().strftime("%H:%M"), C["muted"]) + " "
        if vlen(left) + vlen(right) + 2 > w - 1:
            right = paint(datetime.now().strftime("%H:%M"), C["muted"]) + " "
        line1 = left + " " * max(1, w - 1 - vlen(left) - vlen(right)) + right

        tabs, spans, x, under = " ", [], 1, None
        for i, (key, label) in enumerate(TABS, 1):
            if key == self.tab:
                t = paint(f" {i} ", C["ink"], True, C["accent"]) + paint(f" {label} ", C["text"], True)
                under = (x, x + vlen(t))
            else:
                t = paint(f" {i} ", C["muted"], True, BG["chip"]) + paint(f" {label} ", C["muted"])
            spans.append((x, x + vlen(t), (lambda k: lambda dbl: self.go_tab(k))(key)))
            tabs += t + "  "
            x += vlen(t) + 2
        rule = paint("─" * under[0], C["line"]) + paint("━" * (under[1] - under[0]), C["accent"]) + \
            paint("─" * max(0, w - 1 - under[1]), C["line"])
        return [Line(band(line1, w - 1, BG["panel"])), Line(tabs, spans), Line(rule)]

    def frame(self):
        w, h = self.con.dims()
        top = self.header(w)
        self.focus_line = None
        body = {"live": self.live_lines, "players": self.players_lines, "saves": self.saves_lines,
                "options": self.options_lines}[self.tab](w)

        foot = [paint("─" * (w - 1), C["line"])]
        if self.dialog:
            foot[0] = paint("━" * (w - 1), C["warn"])
            foot += self.dialog["lines"]()
        elif self.busy:
            foot.append(" " + paint(self.busy, C["info"], True))
        elif self.msg and time.time() < self.msg_until:
            col, icon = {"ok": (C["good"], "√"), "error": (C["bad"], "x"), "warn": (C["warn"], "!"),
                         "avoid": (C["accent"], "!"), "match": (C["warn"], "●")}.get(self.msg_kind, (C["info"], "·"))
            foot.append(" " + paint(f" {icon} ", C["ink"], True, col) + " " + paint(self.msg, col, True))
        else:
            foot.append(self.keys_hint())
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
            more = f" {off + 1}-{off + len(view)} of {len(body)} "
            foot[0] = paint(ANSI.sub("", foot[0])[:max(0, w - 2 - len(more))], C["warn"] if self.dialog else C["line"]) + \
                paint(more, C["muted"])
        lines = top + view + [""] * (room - len(view)) + foot
        self.hits = [(y, x0, x1, act) for y, ln in enumerate(lines) for x0, x1, act in getattr(ln, "spans", ())]
        return [str(ln) for ln in lines]

    def keys_hint(self):
        k = []
        if self.tab == "live":
            b = self.live.board
            if b and b["can_dodge"]:
                k.append(("D", "dodge", "d", "danger"))
            if b:
                k.append(("↑↓", "player", "down"))
                if self.pick:
                    k += [("N", "note", "n"), ("T", "tag", "t"), ("Esc", "close", "\x1b")]
            else:
                k.append(("R", "refresh", "r"))
        elif self.tab == "players":
            k.append(("↑↓", "pick", "down"))
            if self.visible_people():
                k += [("N", "note", "n"), ("T", "tag", "t"), ("Del", "clear", "del")]
            k.append(("/", "search", "/"))
        elif self.tab == "saves":
            row = self.save_rows()[self.sel["saves"]] if self.saves else None
            if row is None or row.get("new"):
                k += [("↑↓", "pick", "down"), ("Enter", "save my settings", "\r", "primary")]
            else:
                k += [("↑↓", "pick", "down"), ("Enter", "load", "\r", "primary"), ("Del", "delete", "del")]
                if self.saves.get("can_undo"):
                    k.append(("U", "undo last load", "u"))
        else:
            k += [("↑↓", "pick", "down"), ("Enter", "change", "\r", "primary")]
        k += [("Tab", "screens", "\t"), ("Q", "quit", "q")]
        return keys_line(k)

    # ---- live: the match board, or your session while you're in the menus

    def live_lines(self, w):
        b = self.live.board
        return self.board_lines(b, w) if b else self.session_lines(w)

    def session_lines(self, w):
        st, ses = self.live.status, self.live.session
        if st["riot"] == "starting":
            title, text = "Connecting...", "Looking for the Riot Client."
        elif st["riot"] != "ok":
            title, text = "Can't reach your Riot account", st["message"] + \
                "  valset keeps trying, so this clears up once the Riot Client is signed in."
        elif not st["game_open"]:
            title, text = "VALORANT is closed", "Start the game. Your match shows up here the moment you hit agent select."
        else:
            title, text = "Waiting for a match", "Queue up. The board fills in by itself when agent select starts."
        out = ["", "  " + paint(title, C["text"], True), "  " + paint(text, C["muted"])]
        if st["riot"] != "ok":
            return out
        if not ses:
            return out + ["", "  " + paint("Loading your recent games...", C["dim"])]

        rk = ses.get("rank")
        if rk:
            out += ["", "  " + paint("YOUR RANK", C["muted"], True)]
            if rk["tier"] > 2:
                line = "  " + paint(rk["name"], rank_col(rk["tier"]), True) + "  " + paint(f"{rk['rr']} RR", C["text"], True)
                if rk["tier"] < 24:
                    nxt = tracker.tier_name(rk["tier"] + 1)
                    line += "   " + bar(rk["rr"] / 100, 24, rank_col(rk["tier"])) + "  " + \
                        paint(f"{100 - rk['rr']} RR to {nxt}", C["muted"])
                out.append(line)
            else:
                out.append("  " + paint("Unranked this act", C["muted"], True))

        last = ses.get("last")
        if last:
            out += ["", "  " + paint("LAST MATCH", C["muted"], True) + "   " + paint(when(last["start"]), C["dim"])]
            if last["queue"] in tracker.NO_RESULT_QUEUES:
                res = chip("DONE", C["ink"], C["muted"])
            else:
                res = chip("VICTORY" if last["won"] else "DEFEAT", C["ink"], C["good"] if last["won"] else C["bad"])
            line = "  " + res + "  " + paint(last["score"], C["text"], True) + "   " + paint(last["map"], C["text"], True) + \
                paint("  ·  " + tracker.QUEUE_NAMES.get(last["queue"], last["queue"] or "Custom"), C["muted"])
            if last["rr"] is not None:
                rr = paint(f"{last['rr']:+d} RR", C["good"] if last["rr"] >= 0 else C["bad"], True)
                line += " " * max(3, min(w - 4, 70) - vlen(line) - vlen(rr)) + rr
            out.append(line)
            place = {1: "MVP", 2: "2nd", 3: "3rd"}.get(last["place"], f"{last['place']}th")
            out.append("  " + paint(fit(last["agent"], 10), C["muted"]) + paint(kda(last), C["text"], True) +
                       paint("   ACS ", C["muted"]) + paint(str(last["acs"]), C["text"], True) +
                       paint("   HS ", C["muted"]) + paint(f"{last['hs']}%" if last["hs"] is not None else "-", C["text"], True) +
                       "   " + (chip(place, C["ink"], C["gold"]) if last["place"] <= 3 else
                                paint(f"{place} of {last['of']} by ACS", C["muted"])))
            tops = []
            for side, label in (("mine", "Best on your team"), ("theirs", "Best enemy")):
                t = last["tops"].get(side)
                if t:
                    who = "you" if t["you"] else (t["name"] or t["agent"])
                    extra = "" if t["you"] else (f" {t['agent']}" if t["name"] else "") + f" {kda(t)} · ACS {t['acs']}"
                    tops.append(paint(label + " ", C["muted"]) + paint(who, C["ally"] if side == "mine" else C["enemy"], True) +
                                paint(extra, C["muted"]))
            if tops:
                out.append("  " + "   ".join(tops))

        games = ses.get("games") or []
        if games:
            kd = f"   K/D {ses['kd']:.2f}" if ses["kd"] is not None else ""
            rr = f"   {ses['rr']:+d} RR" if ses["rr"] is not None else ""
            out += ["", "  " + paint("TONIGHT", C["muted"], True) + "   " +
                    paint(f"{ses['w']}W", C["good"], True) + " " + paint(f"{ses['l']}L", C["bad"], True) +
                    paint(f"{rr}{kd}   {len(games)} game{'s' if len(games) != 1 else ''}", C["muted"])]
            for g in games:
                if not g["counts"]:
                    res = paint(" – ", C["muted"], True, BG["chip"])
                else:
                    res = paint(" W ", C["ink"], True, C["good"]) if g["won"] else paint(" L ", C["ink"], True, C["bad"])
                rr = paint(f"{g['rr']:+d}", C["good"] if g["rr"] >= 0 else C["bad"], True) if g["rr"] is not None else ""
                out.append("  " + res + " " + paint(fit(g["score"], 7), C["text"]) + paint(fit(g["map"], 10), C["text"], True) +
                           paint(fit(g["agent"], 10), C["muted"]) + paint(fit(kda(g), 10), C["text"]) +
                           paint(fit(f"ACS {g['acs']}", 9), C["muted"]) + rr)
        elif not last:
            out += ["", "  " + paint("No games in your recent history yet.", C["dim"])]
        out += ["", "  " + paint("Tip: ", C["dim"]) + paint("in a match, click a player (or ↑↓) to see their last games "
                                                            "and leave a note. Notes come back when you meet them again.",
                                                            C["dim"])]
        return out

    def flat_players(self):
        b = self.live.board
        return [p for t in b["teams"] for p in t["players"]] if b else []

    def board_lines(self, b, w):
        out = [""]
        select = b["phase"] == "agent select"
        head = " " + paint(b["map"].upper(), C["text"], True) + paint(f"   {b['queue_name']}", C["muted"])
        spans = []
        if b["can_dodge"]:
            hk = self.hk_label()
            d = chip("D", C["ink"], C["accent"]) + paint(" dodge", C["text"], True) + \
                paint(f"  or {hk} twice" if hk else "", C["muted"])
            x = max(vlen(head) + 3, w - 2 - vlen(d))
            spans.append((x, x + 8, "d"))
            head += " " * (x - vlen(head)) + d
        out.append(Line(head, spans))
        if self.live.armed and self.hk_label():
            out += ["", paint(fit(f"  Press {self.hk_label()} again to dodge", w - 1), "#ffffff", True, C["accent"])]
        narrow = w < 104
        if b.get("edge") is not None:
            e = b["edge"]
            out.append(" " + paint("Team edge ", C["muted"]) + bar(e / 100, 20 if narrow else 30, C["ally"], C["enemy"], "█") +
                       "  " + paint(f"{e}%", C["ally"], True) + paint(" vs ", C["muted"]) +
                       paint(f"{100 - e}%", C["enemy"], True) +
                       paint("   a rough guess from rank, K/D and win rate" if not narrow else "", C["dim"]))
        skins = self.prefs.get("show_skins") and not self.prefs.get("compact")
        name_w = max(14, min(26, w - 100))
        players = self.flat_players()
        if self.pick and self.pick not in [p["puuid"] for p in players]:
            self.pick = None
        stripe_n = 0
        for t in b["teams"]:
            out.append("")
            col = C["ally"] if t["mine"] else C["enemy"]
            avg = t["avg_rank"]
            label = f" {t['label'].upper()} "
            right = f" avg {avg['name']} " if avg else ""
            out.append(paint("━━", col) + paint(label, col, True) +
                       paint("━" * max(2, w - 5 - len(label) - len(right)), col) +
                       paint(right, rank_col(avg["tier"]) if avg else C["muted"], True))
            if not narrow:
                out.append(paint(f"   {fit('AGENT', 10)}{fit('PLAYER', name_w + 1)}{fit('RANK', 19)}{fit('PEAK', 13)}"
                                 f"{'WIN%':>5} {'K/D':>5} {'HS%':>5}  {fit('FORM', 5)}{fit('PARTY', 11)}NOTES", C["dim"]))
            for p in t["players"]:
                stripe_n += 1
                out += self.player_lines(p, w, name_w, narrow, skins, stripe_n % 2 == 0)
                if p["puuid"] == self.pick:
                    self.focus_line = len(out) - 1  # keep the whole block, details too, on screen
        out += ["", paint(" FORM is their last 3 competitive games. Party: in a party now, or queued together lately.", C["dim"])]
        if select:
            out.append(paint(" The enemy team appears once the match loads.", C["dim"]))
        return out

    def player_lines(self, p, w, name_w, narrow, skins, odd):
        picked = p["puuid"] == self.pick
        party = p["party"]
        pcol = PARTY_COLORS[(party["n"] - 1) % len(PARTY_COLORS)] if party else None
        stripe = paint("▌", pcol) if pcol else " "
        mark = paint("►", C["accent"], True) if picked else " "
        agent = paint(fit(p["agent"], 10), C["muted"])
        name = p["name"] + (" (you)" if p["me"] else "")
        name_s = paint(fit(name, name_w), C["good"] if p["me"] else C["text"], True)
        if p["loading"]:
            rank = paint(fit("loading...", 18), C["dim"])
            peak = paint(fit("", 12), C["dim"])
        else:
            rk, pk = p["rank"], p["peak"]
            rtxt = (f"{rk['name']} {rk['rr']}rr" if rk["tier"] > 2 else "Unranked") if rk else "?"
            rank = paint(fit(rtxt, 18), rank_col(rk["tier"]) if rk else C["muted"], True)
            peak = paint(fit(pk["name"] if pk and pk["tier"] > 2 else "-", 12), rank_col(pk["tier"]) if pk else C["muted"])

        def stat(v, fmt, good, bad, loading):
            if v is None:
                return paint(f"{'...' if loading else '-':>5}", C["dim"])
            col = C["good"] if good(v) else C["bad"] if bad(v) else C["text"]
            return paint(f"{fmt(v):>5}", col, True)
        wr = stat(p["wr"], lambda v: f"{v}%", lambda v: v >= 55 and (p["games"] or 0) >= 5,
                  lambda v: v < 45 and (p["games"] or 0) >= 5, False)
        kd = stat(p["kd"], lambda v: f"{v:.2f}", lambda v: v >= 1.2, lambda v: v < 0.9, p["stats_loading"])
        hs = stat(p["hs"], lambda v: f"{v}%", lambda v: v >= 28, lambda v: v < 15, p["stats_loading"])
        form = "".join(paint(c, C["good"] if c == "W" else C["bad"], True) for c in p["form"][:3]) + \
            " " * (3 - len(p["form"][:3])) if p["form"] else paint("...", C["dim"]) if p["stats_loading"] else paint("-  ", C["dim"])
        if party:
            tag = paint(fit(f" {party['label'].upper()} {party['n']} ", 10), C["ink"], True, pcol)
        else:
            tag = paint(fit(" solo" if p["solo"] else "", 10), C["muted"])
        notes = self.note_chips(p)
        if narrow:
            lines = [f"{mark}{stripe} {name_s} {paint(p['agent'], C['muted'])}  {tag}",
                     f" {stripe}   {rank} {paint('peak', C['dim'])} {peak} "
                     f"{paint('WR', C['dim'])}{wr} {paint('KD', C['dim'])}{kd} {paint('HS', C['dim'])}{hs}  {form}"]
            if notes:
                lines.append(f" {stripe}   {notes}")
        else:
            lines = [f"{mark}{stripe} {agent}{name_s} {rank} {peak} {wr} {kd} {hs}  {form}  {tag} {notes}"]
        if skins and p["skins"]:
            sk = "   ".join(f"{k['weapon']}: {k['name']}" for k in p["skins"])
            lines.append(f" {stripe}" + paint(("   " if narrow else " " * 12) + sk, C["dim"]))
        if picked:
            lines += self.player_detail(p, stripe)
        pu = p["puuid"]
        act = lambda dbl: self.pick_player(None if self.pick == pu and not dbl else pu)
        rows = []
        for i, ln in enumerate(lines):
            if picked:
                ln = band(ln, w - 1, BG["sel"])
            elif odd and self.prefs.get("theme_bg", True):
                ln = band(ln, w - 1, BG["stripe"])
            rows.append(Line(ln, [(0, w, act)]))
        return rows

    def note_chips(self, p):
        memo = p.get("memo") or {}
        out = []
        if memo.get("tag") == "avoid":
            out.append(chip("AVOID", C["ink"], C["accent"]))
        elif memo.get("tag") == "good":
            out.append(chip("GOOD", C["ink"], C["good"]))
        if memo.get("met"):
            out.append(paint(f"met {memo['met']}×", C["info"], True))
        for kind, text in p.get("flags") or []:
            col = {"hot": C["warn"], "cold": C["info"], "smurf": C["accent"], "main": C["muted"]}[kind]
            icon = {"hot": "▲", "cold": "▼"}.get(kind, "")
            out.append(paint(icon + text, col, kind != "main"))
        if memo.get("note"):
            out.append(paint(f"\"{memo['note']}\"", C["muted"], italic=True))
        return "  ".join(out)

    def player_detail(self, p, stripe):
        """The extra lines under the highlighted player."""
        lab = lambda t: paint(fit(t, 12), C["dim"], True)
        pad = f" {stripe}   "
        out = []
        if p["recent"]:
            games = []
            for g in p["recent"]:
                res = paint("W", C["good"], True) if g["won"] else paint("L", C["bad"], True)
                games.append(res + " " + paint(g["agent"], C["text"]) + paint(f" {g['map']} {kda(g)}", C["muted"]))
            out.append(pad + lab("last games") + "   ".join(games))
        elif p["stats_loading"]:
            out.append(pad + lab("last games") + paint("loading...", C["dim"]))
        else:
            out.append(pad + lab("last games") + paint("no competitive games lately", C["dim"]))
        if not p["me"]:
            memo = p.get("memo") or {}
            if memo.get("met"):
                last = memo["last"]
                side = "teammate" if last["same"] else "enemy"
                res = "" if last.get("won") is None else (", you won" if last["won"] else ", you lost")
                out.append(pad + lab("met before") + paint(f"{memo['met']}×", C["info"], True) +
                           paint(f"   last {when(last['t'])} as {side}" + (f" ({last['agent']})" if last.get("agent") else "") +
                                 res + f"   teammate {memo['with']}×, enemy {memo['against']}×", C["muted"]))
            else:
                out.append(pad + lab("met before") + paint("first time you've played with them", C["dim"]))
            tag = {"good": chip("GOOD", C["ink"], C["good"]), "avoid": chip("AVOID", C["ink"], C["accent"])}.get(memo.get("tag"), "")
            note = paint(f"\"{memo['note']}\"", C["text"], italic=True) if memo.get("note") else paint("no note yet", C["dim"])
            out.append(pad + lab("your note") + (tag + " " if tag else "") + note + "     " +
                       paint("N", C["warn"], True) + paint(" write   ", C["muted"]) +
                       paint("T", C["warn"], True) + paint(" tag good / avoid", C["muted"]))
        acct = []
        if p["level"]:
            acct.append(f"level {p['level']}")
        if p["games"] is not None:
            acct.append(f"{p['games']} comp games this act")
        if p["main"]:
            acct.append(f"plays {p['main']} most")
        if acct:
            out.append(pad + lab("account") + paint(" · ".join(acct), C["muted"]))
        return out

    def pick_player(self, puuid):
        self.pick = puuid

    def move_pick(self, step):
        ids = [p["puuid"] for p in self.flat_players()]
        if not ids:
            return
        if self.pick not in ids:
            self.pick = ids[0 if step > 0 else -1]
        else:
            self.pick = ids[max(0, min(len(ids) - 1, ids.index(self.pick) + step))]

    def picked_row(self):
        return next((p for p in self.flat_players() if p["puuid"] == self.pick), None)

    def edit_note(self, puuid, label, current):
        def done(text):
            people.set_note(puuid, text)
            self.after_people_change()
            self.toast(f"Saved your note on {label}." if text else f"Cleared your note on {label}.", "ok")
        self.ask_text(f"Note on {label}", done, current, "shows next time you meet them")

    def tag_player(self, puuid, label):
        tag = people.cycle_tag(puuid)
        self.after_people_change()
        self.toast({"good": f"Tagged {label} good.", "avoid": f"Tagged {label} avoid. valset warns you when you meet them.",
                    "": f"Removed the tag on {label}."}[tag], "ok")

    def after_people_change(self):
        self.live.notes_changed()
        self.load_people()

    # ---- players you've met

    def load_people(self):
        self.people = people.listing()
        self.people_v += 1

    def visible_people(self):
        q = self.filter.lower()
        out = []
        for x in self.people:
            if not x["name"] and not (x["note"] or x["tag"]):
                continue  # only ever seen in match data, never by name
            if q and q not in (x["name"] or "").lower() and q not in x["note"].lower():
                continue
            out.append(x)
        return out

    def players_lines(self, w):
        rows = self.visible_people()
        named = sum(1 for x in self.people if x["name"] or x["note"] or x["tag"])
        tagged = sum(1 for x in self.people if x["tag"] or x["note"])
        out = ["", " " + paint("Players you've met", C["text"], True) +
               paint(f"   {named} player{'s' if named != 1 else ''} · {tagged} with a tag or note", C["muted"])]
        if self.filter:
            out.append(" " + paint("search: ", C["muted"]) + paint(self.filter, C["warn"], True) +
                       paint("   (/ to change, Esc to clear)", C["dim"]))
        out.append(" " + paint("Tag someone good or avoid and add a note. It shows on the live board when you meet them again.",
                               C["dim"]))
        if not rows:
            return out + ["", "   " + paint("Nobody here yet. Play a match with valset open and the people in it show up here, "
                                            "named the way the game showed them."
                                            if not self.filter else "No one matches that search.", C["muted"])]
        sel = self.sel["players"] = max(0, min(self.sel["players"], len(rows) - 1))
        name_w = max(16, min(28, w - 80))
        out += ["", paint(f"  {fit('', 9)}{fit('PLAYER', name_w + 1)}{fit('MET', 6)}{fit('LAST TIME', 40)}NOTE", C["dim"])]
        for i, x in enumerate(rows):
            tag = {"good": chip("GOOD", C["ink"], C["good"]), "avoid": chip("AVOID", C["ink"], C["accent"])}.get(x["tag"], "")
            tag += " " * (9 - vlen(tag))
            name = x["name"] or "hidden player"
            last = x["met"][0] if x["met"] else None
            if last:
                res = "" if last.get("won") is None else (" · won" if last["won"] else " · lost")
                lt = f"{when(last['t'])} · {'teammate' if last['same'] else 'enemy'}{res}" + \
                    (f" · {last['agent']}" if last.get("agent") else "")
            else:
                lt = ""
            text = tag + paint(fit(name, name_w), C["text"], True) + " " + paint(fit(f"{len(x['met'])}×", 6), C["info"]) + \
                paint(fit(lt, 38), C["muted"]) + "  " + paint(x["note"], C["text"], italic=True)
            if i == sel:
                self.focus_line = len(out)
            out.append(self.row_line(i == sel, text, w, "players", i))
        return out

    def players_key(self, c):
        rows = self.visible_people()
        if c in ("up", "down", "home", "end", "pgup", "pgdn"):
            step = {"up": -1, "down": 1, "home": -10 ** 6, "end": 10 ** 6, "pgup": -10, "pgdn": 10}[c]
            self.sel["players"] = max(0, min(len(rows) - 1, self.sel["players"] + step))
            return
        if c == "/":
            def done(text):
                self.filter = text
                self.sel["players"] = 0
            return self.ask_text("Search players by name or note", done, self.filter)
        if c == "\x1b" and self.filter:
            self.filter = ""
            return
        if not rows:
            return
        x = rows[min(self.sel["players"], len(rows) - 1)]
        label = x["name"] or "this player"
        if c in ("n", "\r", "\n", " "):
            self.edit_note(x["puuid"], label, x["note"])
        elif c == "t":
            self.tag_player(x["puuid"], label)
        elif c in ("del", "x", "\x08") and (x["note"] or x["tag"]):
            self.confirm(f"Clear your note and tag on {label}?",
                         lambda: (people.forget(x["puuid"]), self.after_people_change(), self.toast("Cleared.", "ok")))

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

    def row_line(self, selected, text, w, tab=None, idx=None):
        """A list row; the highlighted one gets a bar and a ► marker. Clicking selects it,
        clicking it again (or double-clicking) opens it."""
        if selected:
            ln = band(paint("►", C["accent"], True) + " " + text, w - 1, BG["sel"])
        else:
            ln = "  " + text
        if tab is None:
            return ln
        return Line(ln, [(0, w, lambda dbl: self.click_row(tab, idx, dbl))])

    def click_row(self, tab, idx, dbl):
        if self.sel.get(tab) == idx or dbl:
            self.sel[tab] = idx
            self.on_key("\r")
        else:
            self.sel[tab] = idx

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
                                 paint("   sens, keybinds, crosshair, audio, video...", C["muted"]), w, "saves", 0))
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
                when_s = ""
                try:
                    when_s = datetime.fromisoformat(x["created"]).strftime("%b %d, %H:%M")
                except (TypeError, ValueError):
                    pass
                extra = "" if x["mine"] else f"from {x['account']}   "
                text = paint(fit(x["label"], name_w), C["text"], True) + "  " + \
                    paint(extra + when_s + ("   + video" if x["has_video"] else ""), C["muted"])
                if i == sel:
                    self.focus_line = len(out)
                out.append(self.row_line(i == sel, text, w, "saves", i))
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
        self.ask_text("Name this save", lambda label: self.run_action("Saving your settings...", lambda: app.do_save(label)),
                      hint=f"just Enter = \"{d['account']}\"")

    @staticmethod
    def diff_text(diff):
        parts = []
        for x in diff:
            if x["n"] is None:
                parts.append(paint(f"{x['name']} ", C["dim"]) + paint(x["detail"], C["dim"]))
            elif x["n"] == 0:
                parts.append(paint(f"{x['name']} ", C["muted"]) + paint("same", C["dim"]))
            else:
                parts.append(paint(f"{x['name']} ", C["text"], True) +
                             paint(x["detail"] or f"{x['n']} change{'s' if x['n'] != 1 else ''}", C["warn"], True))
        return paint(" · ", C["dim"]).join(parts)

    def load_save(self, x):
        d = self.saves
        if not d["account"]:
            return self.toast("Sign in to the Riot Client first.", "warn")
        if self.live.status.get("game_open"):
            return self.toast("Close VALORANT first. The game overwrites your settings when it closes.", "warn")
        parts = [c for c, _ in d["parts"]]
        diff = {"rows": None, "error": None}

        def got(rows):
            diff["rows"] = rows
            self.dialog_v += 1

        def failed(m):
            diff["error"] = m
            self.dialog_v += 1
        self.bg(lambda: app.do_diff(x["id"]), got, failed)

        def go(chosen):
            self.close_dialog()
            self.run_action("Loading...", lambda: app.do_load(x["id"], chosen))

        def lines():
            out = [" " + paint(f"Load \"{x['label']}\" onto {d['account']}?", C["text"], True)]
            if diff["error"]:
                out.append(" " + paint("Couldn't compare: " + diff["error"], C["muted"]))
            elif diff["rows"] is None:
                out.append(" " + paint("Checking what would change...", C["info"]))
            elif all(r["n"] == 0 or r["n"] is None for r in diff["rows"]):
                out.append(" " + paint("This save matches your current settings. Nothing would change.", C["good"], True))
            else:
                out.append(" " + paint("Changes  ", C["muted"]) + self.diff_text(diff["rows"]))
            out.append(" " + paint("Your current settings are backed up first, so U can undo it.", C["dim"]))
            out.append(keys_line([("Enter", "load everything", "\r", "primary"), ("P", "choose parts", "p"),
                                  ("Esc", "cancel", "\x1b")]))
            return out

        def on_key(ch):
            if ch in ("\r", "\n"):
                go(parts)
            elif isinstance(ch, str) and ch.lower() == "p":
                self.pick_parts(x, go, diff)
            else:
                self.close_dialog()
                self.toast("Cancelled.", "info")
        self.open_dialog(lines, on_key)

    def pick_parts(self, x, go, diff):
        names = self.saves["parts"]  # [(category, readable name)]
        changed = {r["cat"]: r["n"] for r in diff.get("rows") or []}
        # start with just the parts that would change, when we know them
        on = {c: (changed.get(c) or 0) > 0 if changed and any(changed.values()) else True for c, _ in names}

        def lines():
            boxes = []
            for i, (c, n) in enumerate(names, 1):
                cnt = changed.get(c)
                extra = "" if c not in changed else paint(" (same)" if cnt == 0 else "" if cnt is None else f" ({cnt})", C["dim"])
                boxes.append(paint(f"{i}", C["accent"], True) + " " +
                             (paint("■ " + n, C["text"], True) if on[c] else paint("□ " + n, C["muted"])) + extra)
            return [" " + paint(f"Which parts of \"{x['label']}\"?", C["text"], True) +
                    paint("   press a number to tick or untick it", C["muted"]),
                    " " + "   ".join(boxes),
                    keys_line([("Enter", "load the ticked parts", "\r", "primary"), ("Esc", "cancel", "\x1b")])]

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
            upd = bar(self.update_pct, 20, C["info"]) + paint(f" {self.update_pct * 100:3.0f}%", C["muted"])
        elif self.update_state == "done":
            upd = paint(self.update_msg, C["good"], True)
        elif self.update:
            upd = paint(f"{self.update['tag']} is out", C["info"], True)
        else:
            upd = paint(self.update_msg or f"you have v{VERSION}", C["muted"])
        toggle = lambda key, default=False: (lambda: self.set_pref(key, not p.get(key, default)))
        return [
            ("BOARD", None, None, None),
            ("Compact board", p.get("compact"), "one line per player", toggle("compact")),
            ("Show skins", p.get("show_skins"), "Vandal, Phantom and knife under each player (when compact is off)",
             toggle("show_skins")),
            ("Match alert", p.get("alert", True), "ding and flash the taskbar when agent select starts",
             toggle("alert", True)),
            ("LOOK AND FEEL", None, None, None),
            ("Dark background", p.get("theme_bg", True), "valset's own dark theme (off = your terminal's colours)",
             toggle("theme_bg", True)),
            ("Mouse", p.get("mouse", True), "click tabs, rows and keys; scroll with the wheel", toggle("mouse", True)),
            ("DODGE", None, None, None),
            ("Dodge hotkey", p.get("hotkey_enabled"), f"press {hk} twice in agent select to dodge, even in game",
             toggle("hotkey_enabled")),
            ("Hotkey key", hk, "Enter switches to the next key (" + ", ".join(h["label"] for h in HOTKEYS) + ")",
             self.next_hotkey),
            ("UPDATES", None, None, None),
            ("Check for updates", p.get("auto_update", True), "when valset starts, at most every 6 hours",
             toggle("auto_update", True)),
            ("Install update" if self.update else "Check now", upd, "", self.update_action),
        ]

    def option_items(self):
        return [r for r in self.option_rows() if r[3] is not None]

    def next_hotkey(self):
        labels = [h["label"] for h in HOTKEYS]
        cur = labels.index(self.prefs["hotkey"]["label"]) if self.prefs["hotkey"]["label"] in labels else -1
        self.set_pref("hotkey", HOTKEYS[(cur + 1) % len(HOTKEYS)])
        if not self.hotkey.error:
            self.toast(f"Dodge hotkey is now {self.prefs['hotkey']['label']}.", "ok")

    def options_lines(self, w):
        items = self.option_items()
        sel = self.sel["options"] = max(0, min(self.sel["options"], len(items) - 1))
        out = ["", " " + paint("Options", C["text"], True) + paint("   Enter or click to change", C["muted"])]
        i = -1
        for label, value, desc, _ in self.option_rows():
            if value is None:
                out += ["", " " + paint(label, C["dim"], True)]
                continue
            i += 1
            if value is True or value is False:
                val = paint(" ON  ", C["ink"], True, C["good"]) if value else paint(" OFF ", C["muted"], True, BG["chip"])
            elif isinstance(value, str) and ANSI.search(value):
                val = " " + value
            else:
                val = paint(f" {value} ", C["text"], True, BG["chip"])
            val += " " * max(0, 16 - vlen(val))  # line the descriptions up
            text = paint(fit(label, 20), C["text"], True) + val + "  " + paint(desc, C["muted"])
            if i == sel:
                self.focus_line = len(out)
            out.append(self.row_line(i == sel, text, w, "options", i))
            if label == "Hotkey key" and self.hotkey.error:
                out.append("      " + paint(self.hotkey.error, C["bad"], True))
        if self.update and self.update_state == "idle":
            out += ["", " " + paint(f"What's new in {self.update['tag']}", C["muted"], True)]
            out += ["   " + paint(fit("· " + n, w - 6), C["muted"]) for n in updater.short_notes(self.update, 5)]
        out += ["", " " + paint("Text too small? ", C["muted"]) + paint("Ctrl + mouse wheel", C["text"], True) +
                paint(" makes it bigger.", C["muted"]),
                " " + paint("Second monitor: drag this window there. The board fits narrow windows too.", C["muted"]),
                " " + paint(f"valset v{VERSION}", C["dim"])]
        return out

    # ---- keys and clicks

    def on_mouse(self, ev):
        if ev["mouse"] == "wheel":
            if self.dialog:
                return True
            if self.tab == "live" and not self.pick:
                self.scroll["live"] = max(0, self.scroll["live"] + (-3 if ev["up"] else 3))
                return True
            return self.on_key("up" if ev["up"] else "down")
        for y, x0, x1, act in self.hits:
            if y == ev["y"] and x0 <= ev["x"] < x1:
                if isinstance(act, str):
                    return self.on_key(act)
                if not self.dialog:
                    act(ev["mouse"] == "dclick")
                return True
        return True

    def on_key(self, ch):
        if isinstance(ch, dict):
            return self.on_mouse(ch)
        if self.dialog:
            self.dialog["on_key"](ch)
            return True
        c = ch.lower() if isinstance(ch, str) and len(ch) == 1 else ch
        if c in ("q", "\x03"):
            return False
        tabs = [t for t, _ in TABS]
        if c in ("1", "2", "3", "4"):
            return self.go_tab(tabs[int(c) - 1])
        if c in ("\t", "btab") or c in ("right", "left"):
            return self.go_tab(tabs[(tabs.index(self.tab) + (-1 if c in ("left", "btab") else 1)) % len(tabs)])
        if self.tab == "live":
            self.live_key(c)
        elif self.tab == "players":
            self.players_key(c)
        elif self.tab == "saves":
            self.saves_key(c)
        elif self.tab == "options":
            items = self.option_items()
            if c in ("up", "down", "home", "end"):
                step = {"up": -1, "down": 1, "home": -99, "end": 99}[c]
                self.sel["options"] = max(0, min(len(items) - 1, self.sel["options"] + step))
            elif c in ("\r", "\n", " "):
                items[self.sel["options"]][3]()
        return True

    def live_key(self, c):
        b = self.live.board
        if c in ("up", "down") and b:
            return self.move_pick(-1 if c == "up" else 1)
        if c in ("pgup", "pgdn", "home", "end", "up", "down"):
            h = self.con.dims()[1] - 8
            delta = {"up": -1, "down": 1, "pgup": -h, "pgdn": h, "home": -10 ** 6, "end": 10 ** 6}[c]
            self.scroll["live"] = max(0, self.scroll["live"] + delta)
            return
        if c == "\x1b":
            self.pick = None
        elif c == "r" and not b:
            self.live.refresh_session()
            self.toast("Refreshing your games...", "info")
        elif c == "d":
            if not b or not b["can_dodge"]:
                self.toast("You can only dodge during agent select.", "warn")
            else:
                self.confirm("Dodge this match?", lambda: self.bg(self.live.dodge, None),
                             detail="You lose RR in competitive and get a queue cooldown.", yes="Y", danger=True)
        elif c in ("n", "t", "\r", "\n"):
            p = self.picked_row()
            if not p and c in ("n", "t"):
                return self.toast("Pick a player first: ↑↓ or click their row.", "info")
            if not p or p["me"]:
                return
            if c == "t":
                self.tag_player(p["puuid"], p["name"])
            elif c == "n":
                self.edit_note(p["puuid"], p["name"], (p.get("memo") or {}).get("note", ""))

    def go_tab(self, tab):
        self.tab = tab
        if tab == "saves":
            self.load_saves()  # pick up saves made elsewhere and the current account
        if tab == "players":
            self.load_people()
        return True

    # ---- main loop

    def on_events(self):
        for kind, text in self.live.take_events():
            self.toast(text, kind)
            if kind == "match" and self.prefs.get("alert", True):
                threading.Thread(target=app.attention, daemon=True).start()
            elif kind == "avoid":
                threading.Thread(target=app.beep, args=(0,), daemon=True).start()

    def window_title(self):
        b = self.live.board
        if b:
            t = f"valset · {'Agent select' if b['phase'] == 'agent select' else 'In game'} · {b['map']}"
        else:
            t = "valset"
        if t != self.title_text:
            self.title_text = t
            self.con.title(t)

    def run(self):
        if os.name != "nt" or not sys.stdin.isatty():
            raise AppError("The valset screen needs a real Windows console. Try: valset live")
        self.con = Console(self.prefs.get("mouse", True), self.prefs.get("theme_bg", True))
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
                self.on_events()
                self.window_title()
                state = (self.live.version, self.tab, self.scroll[self.tab], self.msg if time.time() < self.msg_until else "",
                         id(self.dialog), self.dialog_v, self.busy, tuple(self.sel.values()), id(self.saves),
                         self.con.dims(), tuple(sorted((k, str(v)) for k, v in self.prefs.items())), self.hotkey.error,
                         self.update_state, round(self.update_pct, 2), id(self.update), self.pick, self.people_v,
                         self.filter, datetime.now().strftime("%H:%M"), self.live.armed)
                if state != seen:
                    seen = state
                    frame = self.frame()
                    if frame != last_frame or not self.con.prev:
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
