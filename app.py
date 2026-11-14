"""valset app core: background match polling, the dodge hotkey, prefs and save actions.
The screen itself is in tui.py; this file has no UI code.
"""
from __future__ import annotations
import ctypes
import json
import os
import shutil
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes

import tracker
from valset import (APP_DIR, CATEGORIES, CAT_NAMES, AppError, all_snapshots, apply_snapshot,
                    game_running, get_session, resolve, save_snapshot, undo_backups)

PREFS_FILE = APP_DIR / "valset-window.json"
DEFAULT_PREFS = {
    "compact": False, "show_skins": True, "auto_update": True,
    "hotkey_enabled": True, "hotkey": {"ctrl": True, "alt": False, "shift": True, "vk": 0x44, "label": "Ctrl+Shift+D"},
}
ARM_SECONDS = 3  # second hotkey press must come within this


def load_prefs():
    try:
        return {**DEFAULT_PREFS, **json.loads(PREFS_FILE.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return dict(DEFAULT_PREFS)


def save_prefs(p):
    try:
        PREFS_FILE.write_text(json.dumps(p, indent=2), encoding="utf-8")
    except OSError:
        pass


# --------------------------------------------------------------------------- live match

class Live:
    """Polls the Riot Client on a background thread. The window reads `board`/`status` and
    only redraws when `version` changes, so an idle window costs nothing.

    Speed: per match, names and loadouts are fetched once (loadouts again when the game
    loads), parties every 10 s, and each tick is just the two "where am I" calls. Player
    ranks load first on their own pool, the slower K/D/HS% stats on another, and finished
    matches are cached on disk by tracker.Api.details."""

    def __init__(self):
        self.lock = threading.Lock()
        self.version = 0
        self.status = {"riot": "starting", "message": "Connecting to the Riot Client...",
                       "account": None, "game_open": False}
        self.board = None
        self.api = None
        self.api_made = 0
        self.armed_until = 0
        self.events = []  # (kind, text) waiting for the window to show
        self.rank_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rank")
        self.stats_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="stats")
        self._new_match(None)
        threading.Thread(target=self.loop, daemon=True, name="live").start()
        threading.Thread(target=tracker.prune_match_cache, daemon=True).start()

    def _new_match(self, mid):
        self.mid, self.cache, self.queued = mid, {}, set()
        self.names, self.loadouts, self.loadout_phase = {}, {}, None
        self.parties, self.parties_at = {}, 0
        self.my_party = None  # your own party, fetched once per match
        self.players = []

    def bump(self):
        with self.lock:
            self.version += 1

    def event(self, kind, text):
        with self.lock:
            self.events.append((kind, text))
            self.version += 1

    def take_events(self):
        with self.lock:
            ev, self.events = self.events, []
        return ev

    def get_api(self, max_age=300):
        # tokens last about an hour; a fresh session every few minutes keeps them valid
        if self.api is None or time.time() - self.api_made > max_age:
            s = get_session()
            if self.api is None or self.api.s.puuid != s.puuid:
                self._new_match(None)
            self.api = tracker.Api(s)
            self.api_made = time.time()
        return self.api

    def set_status(self, **kw):
        new = {**self.status, **kw}
        if new != self.status:
            with self.lock:
                self.status = new
                self.version += 1

    def set_board(self, board):
        if board != self.board:
            with self.lock:
                self.board = board
                self.version += 1

    def loop(self):
        while True:
            try:
                wait = self.tick()
            except Exception:
                traceback.print_exc()
                wait = 3
            time.sleep(wait)

    def tick(self):
        game = game_running()
        try:
            # game closed: only re-check who's signed in once a minute
            api = self.get_api(300 if game else 60)
        except AppError as e:
            self.api = None
            self.set_status(riot="error", message=str(e), account=None, game_open=game)
            self.set_board(None)
            return 3
        self.set_status(riot="ok", message="Connected", account=api.s.display, game_open=game)
        if not game:
            self.set_board(None)
            return 3
        # in the menus, the local Riot Client says so: skip Riot's servers entirely
        if tracker.my_loop_state(api.s.puuid) == "MENUS":
            self.set_board(None)
            return 1
        try:
            phase, mid, map_name, queue, players = api.match()
        except AppError as e:
            self.api = None
            self.set_status(riot="error", message=str(e))
            return 3
        if not phase:
            self.set_board(None)
            return 1  # in the menus: look often so agent select shows up right away
        if mid != self.mid:
            self._new_match(mid)
        self.players = players
        puuids = [p["puuid"] for p in players]

        # ranks first for everyone, then the slower stats
        fresh = [pu for pu in puuids if pu not in self.queued]
        for pu in fresh:
            self.queued.add(pu)
            self.rank_pool.submit(self._load_rank, api, pu, mid)
        for pu in fresh:
            self.stats_pool.submit(self._load_stats, api, pu, mid)

        missing = [pu for pu in puuids if pu not in self.names]
        if missing:
            got = api.names(missing)
            for pu in missing:
                self.names[pu] = got.get(pu, "Unknown")
        if self.loadout_phase != phase or (len(self.loadouts) < len(puuids)):
            self.loadouts = api.loadouts(phase, mid, players)
            self.loadout_phase = phase
        if self.my_party is None:
            try:
                self.my_party = api.my_party()
            except AppError:
                self.my_party = {}
        if time.time() - self.parties_at > 10:
            # friends' presences, then your own party on top (exact even for non-friends)
            self.parties, self.parties_at = {**tracker.party_ids(), **self.my_party}, time.time()

        snap = {"phase": phase, "mid": mid, "map": map_name, "queue": queue, "players": players,
                "names": self.names, "parties": self.parties, "loadouts": self.loadouts}
        self.set_board(tracker.board(api, snap, self.cache))
        # agent select changes by the second; in game the board is fixed once everyone's loaded,
        # so just check now and then for the match ending
        loading = any(pu not in self.cache or not self.cache[pu].get("stats_done") for pu in puuids)
        return 1.5 if phase == "agent select" or loading else 6

    def _load_rank(self, api, pu, mid):
        try:
            m = api.mmr(pu)
        except Exception:
            m = None
        if mid == self.mid:
            self.cache.setdefault(pu, {})["mmr"] = m
            self._rebuild()

    def _load_stats(self, api, pu, mid):
        if mid != self.mid:
            return
        try:
            st = tracker.recent_stats(api, pu)
        except Exception:
            st = {"kd": None, "hs": None, "form": "", "party_mates": set(), "stats_done": True}
        if mid == self.mid:
            self.cache.setdefault(pu, {}).update(st)
            self._rebuild()

    def _rebuild(self):
        """Redraw with new player data right away instead of waiting for the next tick."""
        b = self.board
        if b and self.api:
            snap = {"phase": b["phase"], "mid": b["mid"], "map": b["map"], "queue": b["queue"],
                    "players": self.players, "names": self.names, "parties": self.parties,
                    "loadouts": self.loadouts}
            try:
                self.set_board(tracker.board(self.api, snap, self.cache))
            except Exception:
                pass

    # ---- dodge

    def dodge(self):
        self.get_api().dodge()
        self.armed_until = 0
        self.event("ok", "Dodged. You're out of agent select.")

    def hotkey_pressed(self):
        try:
            in_select = self.get_api().in_agent_select()
        except AppError:
            in_select = False
        if not in_select:
            self.armed_until = 0
            self.event("info", "The dodge hotkey only works in agent select.")
            return
        if self.armed_until > time.time():
            try:
                self.dodge()
                beep(1)
            except AppError as e:
                self.armed_until = 0
                self.event("error", str(e))
        else:
            self.armed_until = time.time() + ARM_SECONDS
            beep(0)
            self.event("warn", f"Press the dodge hotkey again within {ARM_SECONDS} seconds to dodge.")
            threading.Timer(ARM_SECONDS + 0.1, self.bump).start()  # clear the banner when it runs out

    @property
    def armed(self):
        return self.armed_until > time.time()


def beep(kind):
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION if kind == 0 else winsound.MB_OK)
    except Exception:
        pass


# --------------------------------------------------------------------------- global hotkey

class Hotkey:
    """System-wide hotkey via RegisterHotKey (works while the game has focus)."""
    WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
    MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000

    def __init__(self, on_press):
        self.on_press = on_press
        self.thread_id = None
        self.thread = None
        self.error = None

    def start(self, spec):
        self.stop()
        if os.name != "nt" or not spec:
            return
        ready = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(spec, ready), daemon=True, name="hotkey")
        self.thread.start()
        ready.wait(2)

    def stop(self):
        if self.thread_id:
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, self.WM_QUIT, 0, 0)
            self.thread.join(2)
        self.thread_id = self.thread = None

    def _run(self, spec, ready):
        user32 = ctypes.windll.user32
        self.thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        mods = self.MOD_NOREPEAT | (self.MOD_CONTROL if spec.get("ctrl") else 0) \
            | (self.MOD_ALT if spec.get("alt") else 0) | (self.MOD_SHIFT if spec.get("shift") else 0)
        if not user32.RegisterHotKey(None, 1, mods, int(spec["vk"])):
            self.error = f"{spec.get('label', 'That key')} is already used by another app. Pick a different one."
            self.thread_id = None
            ready.set()
            return
        self.error = None
        ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:  # sleeps until a key or quit
            if msg.message == self.WM_HOTKEY:
                threading.Thread(target=self.on_press, daemon=True).start()
        user32.UnregisterHotKey(None, 1)


# --------------------------------------------------------------------------- settings saves

def saves_data():
    try:
        s = get_session()
    except AppError:
        s = None
    me = s.puuid if s else None
    out = [{"id": x.id, "label": x.label or x.id, "account": x.account, "mine": x.puuid == me,
            "created": x.meta.get("created"), "auto": bool(x.meta.get("auto")),
            "has_video": bool(x.meta.get("video_folders"))}
           for x in all_snapshots(me)]
    return {"account": s.display if s else None, "saves": out, "can_undo": bool(s and undo_backups(s)),
            "parts": [(c, CAT_NAMES[c]) for c in CATEGORIES]}


def do_save(label):
    s = get_session()
    snap = save_snapshot(s, (label or "").strip() or s.display)
    return f"Saved \"{snap.label}\" for {s.display}."


def do_load(sid, parts):
    s = get_session()
    snap = resolve(sid, s.puuid)
    parts = [p for p in parts if p in CATEGORIES] or list(CATEGORIES)
    r = apply_snapshot(s, snap, parts)
    text = f"Loaded \"{snap.label or snap.id}\" onto {s.display}. Start VALORANT and it's there."
    if r["verified"] is False:
        text += " (Riot's copy doesn't fully match, check it in game.)"
    return text


def do_undo():
    s = get_session()
    backups = undo_backups(s)
    if not backups:
        raise AppError("Nothing to undo on this account.")
    b = backups[0]
    apply_snapshot(s, b, list(CATEGORIES), "before undo")
    shutil.rmtree(b.path, ignore_errors=True)
    return f"Put {s.display} back to how it was before the last load."


def do_delete(sid):
    snap = resolve(sid)
    shutil.rmtree(snap.path)
    return f"Deleted \"{snap.label or snap.id}\"."
