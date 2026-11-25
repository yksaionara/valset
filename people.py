"""Players you've met: who was in your matches, how those games went, and your own notes
and tags on them. Everything stays on this PC in profiles/players.json, next to your saves.

Hidden-name players are kept by their Riot ID only internally (the id Riot's match data
uses); valset never stores or shows the name they hid.
"""
from __future__ import annotations
import json
import threading
import time

from valset import PROFILES

FILE = PROFILES / "players.json"
TAGS = ["", "good", "avoid"]  # cycled with T; "" = no tag
MAX_MET = 25  # encounters kept per player
MAX_PLAYERS = 4000  # untagged, note-less players beyond this are dropped, oldest first

_lock = threading.RLock()
_data = None  # {"v": 1, "players": {puuid: {...}}, "seeded": [match ids from the cache scan]}


def _load():
    global _data
    if _data is None:
        try:
            _data = json.loads(FILE.read_text(encoding="utf-8"))
            if not isinstance(_data.get("players"), dict):
                raise ValueError
        except (OSError, ValueError, AttributeError):
            _data = {"v": 1, "players": {}}
    return _data


def _save():
    d = _load()
    players = d["players"]
    if len(players) > MAX_PLAYERS:
        loose = sorted((p for p in players if not players[p].get("note") and not players[p].get("tag")),
                       key=lambda p: players[p].get("last", 0))
        for p in loose[:len(players) - MAX_PLAYERS]:
            players.pop(p, None)
    try:
        PROFILES.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, separators=(",", ":")), encoding="utf-8")
        tmp.replace(FILE)
    except OSError:
        pass


def get(puuid):
    with _lock:
        return dict(_load()["players"].get(puuid) or {})


def _entry(puuid):
    return _load()["players"].setdefault(puuid, {"met": []})


def record(mid, start_ms, rows, save=True):
    """Remember one match. rows: [{puuid, same_team, agent, map, queue, name (None if hidden),
    won (from your side, or None if not known yet)}]. Adding a match twice only fills in gaps."""
    with _lock:
        for r in rows:
            e = _entry(r["puuid"])
            if r.get("name"):
                e["name"] = r["name"]
            hit = next((m for m in e["met"] if m["mid"] == mid), None)
            if hit:
                if hit.get("won") is None and r.get("won") is not None:
                    hit["won"] = r["won"]
                continue
            e["met"].append({"mid": mid, "t": start_ms or int(time.time() * 1000), "same": r["same_team"],
                             "agent": r.get("agent"), "map": r.get("map"), "won": r.get("won")})
            e["met"].sort(key=lambda m: m["t"], reverse=True)
            del e["met"][MAX_MET:]
            e["last"] = max(e.get("last", 0), e["met"][0]["t"])
        if save:
            _save()


def set_result(mid, won):
    """Fill in the result of a match you were in, for everyone you met in it."""
    with _lock:
        changed = False
        for e in _load()["players"].values():
            for m in e.get("met") or []:
                if m["mid"] == mid and m.get("won") is None:
                    m["won"] = won
                    changed = True
        if changed:
            _save()


def set_note(puuid, note):
    with _lock:
        e = _entry(puuid)
        e["note"] = (note or "").strip()[:120]
        _save()


def cycle_tag(puuid):
    with _lock:
        e = _entry(puuid)
        cur = e.get("tag", "")
        e["tag"] = TAGS[(TAGS.index(cur) + 1) % len(TAGS)] if cur in TAGS else ""
        _save()
        return e["tag"]


def forget(puuid):
    """Clear your note and tag (the encounter history stays)."""
    with _lock:
        e = _load()["players"].get(puuid)
        if e:
            e.pop("note", None)
            e.pop("tag", None)
            _save()


def summary(puuid, current_mid=None):
    """What the board shows: times met before this match, the last time, your note and tag."""
    e = get(puuid)
    met = [m for m in e.get("met") or [] if m["mid"] != current_mid]
    return {"met": len(met), "last": met[0] if met else None, "note": e.get("note", ""), "tag": e.get("tag", ""),
            "with": sum(1 for m in met if m["same"]), "against": sum(1 for m in met if not m["same"]),
            "wins_with": sum(1 for m in met if m["same"] and m.get("won"))}


def listing():
    """Everyone worth listing: tagged or noted players first, then the most recently met."""
    with _lock:
        players = {k: dict(v) for k, v in _load()["players"].items()}
    out = []
    for pu, e in players.items():
        if not e.get("met") and not e.get("note") and not e.get("tag"):
            continue
        out.append({"puuid": pu, "name": e.get("name"), "note": e.get("note", ""), "tag": e.get("tag", ""),
                    "met": e.get("met") or [], "last": e.get("last", 0)})
    out.sort(key=lambda x: (not (x["note"] or x["tag"]), -x["last"]))
    return out


def seed_from_cache(my_puuid, matches, agent_names, map_names):
    """One-time fill from finished matches already on disk (the tracker caches every match it
    reads). `matches`: iterable of (match id, compact match). Only matches you played in count."""
    with _lock:
        d = _load()
        done = set(d.get("seeded") or [])
    added = 0
    for mid, m in matches:
        if mid in done:
            continue
        done.add(mid)
        players = m.get("players") or []
        me = next((p for p in players if p.get("subject") == my_puuid), None)
        if not me:
            continue
        teams = {t.get("teamId"): t for t in m.get("teams") or []}
        won = (teams.get(me.get("teamId")) or {}).get("won")
        mi = m.get("matchInfo") or {}
        rows = [{"puuid": p["subject"], "same_team": p.get("teamId") == me.get("teamId"),
                 "agent": agent_names.get((p.get("characterId") or "").lower()),
                 "map": map_names.get(mi.get("mapId")), "won": won}
                for p in players if p.get("subject") and p["subject"] != my_puuid]
        record(mid, mi.get("gameStartMillis"), rows, save=False)
        added += 1
    with _lock:
        _load()["seeded"] = sorted(done)[-3000:]
        _save()
    return added
