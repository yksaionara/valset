"""valset app core: background match polling, your session, the dodge hotkey, prefs and
save actions. The screen itself is in tui.py; this file has no UI code.
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

import people
import tracker
from valset import (APP_DIR, CATEGORIES, CAT_NAMES, AppError, account_config_dirs, all_snapshots, apply_snapshot,
                    flatten, game_running, get_cloud, get_session, read_ini_dir, resolve, save_snapshot, short,
                    undo_backups)

PREFS_FILE = APP_DIR / "valset-window.json"
PREFS_VERSION = 2  # 2: compact became the default
DEFAULT_PREFS = {
    "v": PREFS_VERSION, "compact": True, "show_skins": True, "auto_update": True,
    "hotkey_enabled": True, "hotkey": {"ctrl": True, "alt": False, "shift": True, "vk": 0x44, "label": "Ctrl+Shift+D"},
    "theme_bg": True, "mouse": True,
}
ARM_SECONDS = 3  # second hotkey press must come within this
SESSION_RETRY = 20  # seconds between looks for a just-finished match in your history
SESSION_TRIES = 15  # Riot can take a few minutes to list a finished match


def load_prefs():
    try:
        saved = json.loads(PREFS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(DEFAULT_PREFS)
    if saved.get("v", 1) < 2:  # older copies saved compact=False as the old default
        saved["compact"] = True
    return {**DEFAULT_PREFS, **saved, "v": PREFS_VERSION}


def save_prefs(p):
    try:
        PREFS_FILE.write_text(json.dumps(p, indent=2), encoding="utf-8")
    except OSError:
        pass


# --------------------------------------------------------------------------- live match

class Live:
    """Polls the Riot Client on a background thread. The window reads `board`, `status` and
    `session` and only redraws when `version` changes, so an idle window costs nothing.

    Speed: per match, names and loadouts are fetched once (loadouts again when the game
    loads), parties every 10 s, and each tick is just the two "where am I" calls. Player
    ranks load first on their own pool, the slower K/D/HS% stats on another, and finished
    matches are cached on disk by tracker.Api.details. Your session (tonight's games and the
    last match's recap) is fetched at start and again after each match."""

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
        self.seen_names = {}  # puuid -> name, for players whose names you could see (recap)
        self.recorded = set()  # match ids written to people
        self.alerted = set()  # (match id, puuid) already warned about
        self._new_session()
        self._new_match(None)
        threading.Thread(target=self.loop, daemon=True, name="live").start()
        threading.Thread(target=tracker.prune_match_cache, daemon=True).start()

    def _new_match(self, mid):
        self.mid, self.cache, self.queued = mid, {}, set()
        self.names, self.loadouts, self.loadout_phase, self.loadout_at = {}, {}, None, 0
        self.parties, self.parties_at = {}, 0
        self.my_party = None  # your own party, fetched once per match
        self.players = []

    def _new_session(self):
        self.session = None
        self.session_due, self.session_wait, self.session_tries = time.time(), None, 0
        self.session_busy = False
        self.announce = None  # match id whose result gets a toast once it's in
        self.seeded = False

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
                if self.api is not None:
                    self._new_session()  # another account: its own session and recap
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
            old = self.board
            with self.lock:
                self.board = board
                self.version += 1
            if old and old["phase"] == "in game" and not board:
                self._match_ended(old["mid"])

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
        self._session_tick(api)
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
            if phase == "agent select":
                self.event("match", f"Match found: {map_name}. Agent select has started.")
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
        # Riot can hand back empty loadouts early in agent select, so ask again (every 10s at most)
        # until everyone's skins are in
        have = sum(1 for pu in puuids if self.loadouts.get(pu))
        if self.loadout_phase != phase or (have < len(puuids) and time.time() - self.loadout_at > 10):
            self.loadout_at = time.time()
            try:
                got = api.loadouts(phase, mid, players)
            except AppError:
                got = {}
            if self.loadout_phase != phase or len([v for v in got.values() if v]) >= have:
                self.loadouts = got
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
        b = tracker.board(api, snap, self.cache, self._memo)
        self.set_board(b)
        self._after_board(b)
        # agent select changes by the second; in game the board is fixed once everyone's loaded,
        # so just check now and then for the match ending
        loading = any(pu not in self.cache or not self.cache[pu].get("stats_done") for pu in puuids)
        return 1.5 if phase == "agent select" or loading else 6

    def _memo(self, puuid):
        return people.summary(puuid, self.mid)

    def _after_board(self, b):
        """Remember who's in this match, and warn once about players you tagged 'avoid'."""
        me = self.api.s.puuid if self.api else None
        for t in b["teams"]:
            for r in t["players"]:
                if not r["hidden"] and not r["me"] and r["name"] not in ("Unknown", "Hidden"):
                    self.seen_names[r["puuid"]] = r["name"]
                memo = r.get("memo") or {}
                key = (b["mid"], r["puuid"])
                if memo.get("tag") == "avoid" and key not in self.alerted:
                    self.alerted.add(key)
                    side = "on your team" if t["mine"] else "on the enemy team"
                    note = f": \"{memo['note']}\"" if memo.get("note") else ""
                    self.event("avoid", f"Heads up: {r['name']} ({r['agent']}) {side} is tagged avoid{note}")
        if len(self.seen_names) > 500:
            for k in list(self.seen_names)[:100]:
                self.seen_names.pop(k, None)
        if b["phase"] == "in game" and b["mid"] not in self.recorded and me:
            players = [r for t in b["teams"] for r in t["players"]]
            if len(players) >= 2 and all(r["agent"] != "Picking..." for r in players):
                self.recorded.add(b["mid"])
                rows = [{"puuid": r["puuid"], "same_team": t["mine"], "agent": r["agent"], "map": b["map"],
                         "name": None if r["hidden"] else r["name"], "won": None}
                        for t in b["teams"] for r in t["players"] if not r["me"]]
                threading.Thread(target=people.record, args=(b["mid"], None, rows), daemon=True).start()

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
                nb = tracker.board(self.api, snap, self.cache, self._memo)
                self.set_board(nb)
                self._after_board(nb)
            except Exception:
                pass

    def notes_changed(self):
        """You edited a note or tag: show it on the board now."""
        self._rebuild()
        self.bump()

    # ---- your session and the last match's recap

    def _match_ended(self, mid):
        self.session_wait, self.session_tries, self.announce = mid, 0, mid
        self.session_due = time.time() + 8

    def refresh_session(self):
        self.session_due, self.session_tries = time.time(), 0

    def _session_tick(self, api):
        if self.session_busy or not self.session_due or time.time() < self.session_due:
            return
        self.session_busy, self.session_due = True, 0
        threading.Thread(target=self._load_session, args=(api,), daemon=True, name="session").start()

    def _load_session(self, api):
        try:
            if not self.seeded:  # one-time: everyone you met in matches already on disk
                self.seeded = True
                people.seed_from_cache(api.s.puuid, _cached_matches(), tracker.agents(), tracker.maps())
            s = tracker.session(api, self.seen_names)
            # the games tonight carry their results: fill them in for the players you met
            people.seed_from_cache(api.s.puuid, ((g["mid"], api.details(g["mid"])) for g in s["games"]),
                                   tracker.agents(), tracker.maps())
            if self.api is not None and self.api.s.puuid != api.s.puuid:
                return  # switched accounts meanwhile
            waiting = self.session_wait and s["newest"] != self.session_wait
            if waiting and self.session_tries < SESSION_TRIES:
                self.session_tries += 1
                self.session_due = time.time() + SESSION_RETRY
            else:
                self.session_wait = None
            with self.lock:
                self.session = s
                self.version += 1
            last = s.get("last")
            if self.announce and last and last["mid"] == self.announce:
                self.announce = None
                self.event("ok" if last["won"] else "info",
                           f"Match over: {'won' if last['won'] else 'lost'} {last['score']} on {last['map']}." +
                           (f" {last['rr']:+d} RR." if last["rr"] is not None else ""))
        except AppError:
            self.session_due = time.time() + 60
        except Exception:
            traceback.print_exc()
            self.session_due = time.time() + 120
        finally:
            self.session_busy = False

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


def _cached_matches():
    for f in tracker.MATCH_CACHE.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        yield f.stem, d


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


DIFF_SHORT = {"sens": "Sens", "crosshair": "Crosshair", "keybinds": "Keybinds", "audio": "Audio",
              "general": "General", "video": "Video"}


def _num(v):
    try:
        return f"{float(v):.3f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return short(v, 16)


def do_diff(sid):
    """What loading a save would change on the signed-in account, per part:
    [{"cat", "name", "n" (settings that differ), "detail" (a short 'now → save' for the big ones)}]."""
    s = get_session()
    snap = resolve(sid, s.puuid)
    cur, old = flatten(get_cloud(s)), flatten(snap.cloud())
    out = []
    for c in CATEGORIES:
        if c == "video":
            n, dirs = 0, account_config_dirs(s.puuid)
            src = next((d for d in snap.video_dir.iterdir() if d.is_dir()), None) if snap.video_dir.exists() else None
            if src is None:
                out.append({"cat": c, "name": DIFF_SHORT[c], "n": None, "detail": "not in this save"})
                continue
            if dirs:
                a, b = read_ini_dir(dirs[0]), read_ini_dir(src)
                n = sum(1 for k in set(a) | set(b) if a.get(k) != b.get(k))
            else:
                n = 1
            out.append({"cat": c, "name": DIFF_SHORT[c], "n": n, "detail": ""})
            continue
        keys = [k for k in set(cur) | set(old) if k[0] == c and cur.get(k) != old.get(k)]
        detail = ""
        if c == "sens":
            k = ("sens", "MouseSensitivity")
            if k in keys:
                detail = f"{_num(cur.get(k))} → {_num(old.get(k))}"
        elif c == "crosshair":
            k = ("crosshair", "CrosshairProfileName")
            if k in keys and cur.get(k) and old.get(k):
                detail = f"{short(cur[k], 14)} → {short(old[k], 14)}"
        out.append({"cat": c, "name": DIFF_SHORT[c], "n": len(keys), "detail": detail})
    return out


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
