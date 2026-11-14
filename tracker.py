"""Live match tracker for valset: ranks, peaks, recent form, parties and skins for
everyone in your match, plus your own match history.

Everything comes from the same Riot endpoints the game client uses (through the
Riot Client's login), plus the public valorant-api.com tables for names.
"""
from __future__ import annotations
import base64
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from valset import (LOCALAPPDATA, AppError, HttpError, Session, _pp_headers, clear, dim, get_session, head,
                    http, info, paint, read_lockfile, warn)

SHARD = {"na": "na", "latam": "na", "br": "na", "pbe": "pbe", "eu": "eu", "ap": "ap", "kr": "kr"}
PARTY_COLORS = ["35", "36", "33", "34", "32", "91", "96", "93"]
PARTY_WORD = {2: "duo", 3: "trio", 4: "4-stack", 5: "5-stack"}
SKIN_SOCKET = "bcef87d6-209b-46c6-8b19-fbe40bd95abc"
SHOWN_WEAPONS = [("Vandal", "9c82e19d-4575-0200-1a81-3eacf00cf872"),
                 ("Phantom", "ee8e8d15-496b-07ac-e5f6-8fae5d4c7b1a"),
                 ("Knife", "2f59173c-4bed-b6c3-2191-dea9b58be9c7")]
QUEUE_NAMES = {"competitive": "Comp", "unrated": "Unrated", "swiftplay": "Swift", "spikerush": "Spike",
               "deathmatch": "DM", "hurm": "TDM", "ggteam": "Escalate", "premier": "Premier",
               "": "Custom"}
RECENT_MATCHES = 3  # recent comp games looked at per player for K/D and HS%
MATCH_CACHE = LOCALAPPDATA / "valset" / "matches"
MATCH_FORMAT = 2  # bump when compact_match keeps something new, so old cache files get refetched


def prune_match_cache(days=21):
    """Drop cached matches nobody's recent history will point at any more."""
    cutoff = time.time() - days * 86400
    try:
        for f in MATCH_CACHE.glob("*.json"):
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
    except OSError:
        pass


# --------------------------------------------------------------------------- public name tables

_static = {}


AGENTS_URL = "https://valorant-api.com/v1/agents?isPlayableCharacter=true"
SKINS_URL = "https://valorant-api.com/v1/weapons/skins"
TIERS_URL = "https://valorant-api.com/v1/competitivetiers"
MAPS_URL = "https://valorant-api.com/v1/maps"


def _skin_icon(k):
    lv = (k.get("levels") or [{}])[0]
    return lv.get("displayIcon") or k.get("displayIcon")


# url -> {table name: builder}. One download fills every table built from that list, then the
# raw list is dropped: the skins list alone is tens of MB as Python objects.
# competitive tiers: newest episode's table, so very old peaks (pre-Ascendant) may be off by a name
_TABLES = {
    AGENTS_URL: {"agents": lambda d: {a["uuid"].lower(): a["displayName"] for a in d},
                 "agent_icons": lambda d: {a["uuid"].lower(): a.get("displayIcon") for a in d}},
    SKINS_URL: {"skins": lambda d: {k["uuid"].lower(): k["displayName"] for k in d},
                "skin_icons": lambda d: {k["uuid"].lower(): _skin_icon(k) for k in d}},
    TIERS_URL: {"tiers": lambda d: {t["tier"]: t["tierName"].title() for t in d[-1]["tiers"]},
                "tier_icons": lambda d: {t["tier"]: t.get("smallIcon") for t in d[-1]["tiers"]}},
    MAPS_URL: {"maps": lambda d: {m["mapUrl"]: m["displayName"] for m in d},
               "map_images": lambda d: {m["mapUrl"]: m.get("listViewIcon") or m.get("splash") for m in d}},
}


def _table(key, url):
    if key not in _static:
        try:
            d = (http("GET", url) or {}).get("data") or []
        except Exception:
            return {}  # not cached: try again next time
        for k, fn in _TABLES[url].items():
            try:
                _static[k] = fn(d)
            except Exception:
                _static[k] = {}
    return _static.get(key, {})


def agents(): return _table("agents", AGENTS_URL)
def agent_icons(): return _table("agent_icons", AGENTS_URL)
def skins(): return _table("skins", SKINS_URL)
def skin_icons(): return _table("skin_icons", SKINS_URL)
def tiers(): return _table("tiers", TIERS_URL)
def tier_icons(): return _table("tier_icons", TIERS_URL)
def maps(): return _table("maps", MAPS_URL)
def map_images(): return _table("map_images", MAPS_URL)


def tier_name(t):
    return tiers().get(t, f"tier {t}") if t and t > 2 else "Unranked"


# --------------------------------------------------------------------------- Riot API

class Api:
    def __init__(self, s: Session):
        self.s = s
        region = (s.region or "na").lower()
        shard = SHARD.get(region, region)
        self.glz = f"https://glz-{region}-1.{shard}.a.pvp.net"
        self.pd = f"https://pd.{shard}.a.pvp.net"
        self.shared = f"https://shared.{shard}.a.pvp.net"
        self.h = _pp_headers(s)
        self._act = None
        self.map_id = None
        self._details = {}

    def call(self, method, url, body=None, quiet=True, tries=4):
        """None on 404 (and on any failure when quiet); raises on auth errors.
        Riot rate-limits bursts (HTTP 429), so those are retried with a growing pause."""
        try:
            return http(method, url, self.h, body)
        except HttpError as e:
            if e.status == 429 and tries > 1:
                time.sleep(2 * (5 - tries))
                return self.call(method, url, body, quiet, tries - 1)
            if e.status in (400, 401, 403):
                raise AppError(f"Riot refused the request (HTTP {e.status}). Restart the Riot Client and try again.")
            if e.status == 404 or quiet:
                return None
            raise
        except AppError:
            if quiet:
                return None
            raise

    # ---- current match

    def match(self):
        """(phase, match_id, map name, queue, players) — phase is None when you're in the menus."""
        def rows(plist, team_of):
            return [{"puuid": p["Subject"], "team": team_of(p),
                     "agent": (p.get("CharacterID") or "").lower(),
                     "incognito": (p.get("PlayerIdentity") or {}).get("Incognito", False),
                     "level": None if (p.get("PlayerIdentity") or {}).get("HideAccountLevel")
                     else (p.get("PlayerIdentity") or {}).get("AccountLevel")}
                    for p in plist]

        me = self.call("GET", f"{self.glz}/core-game/v1/players/{self.s.puuid}")
        if me and me.get("MatchID"):
            mid = me["MatchID"]
            m = self.call("GET", f"{self.glz}/core-game/v1/matches/{mid}") or {}
            queue = (m.get("MatchmakingData") or {}).get("QueueID", "")
            self.map_id = m.get("MapID")
            return ("in game", mid, maps().get(m.get("MapID"), "?"), queue,
                    rows(m.get("Players", []), lambda p: p.get("TeamID", "?")))
        me = self.call("GET", f"{self.glz}/pregame/v1/players/{self.s.puuid}")
        if me and me.get("MatchID"):
            mid = me["MatchID"]
            m = self.call("GET", f"{self.glz}/pregame/v1/matches/{mid}") or {}
            ally = m.get("AllyTeam") or {}
            self.map_id = m.get("MapID")
            return ("agent select", mid, maps().get(m.get("MapID"), "?"), m.get("QueueID", ""),
                    rows(ally.get("Players", []), lambda p: ally.get("TeamID", "?")))
        return None, None, None, None, []

    def my_party(self):
        """puuid -> party id for everyone in YOUR party, friends or not (from Riot's party
        service; the party stays together through agent select and the match)."""
        me = self.call("GET", f"{self.glz}/parties/v1/players/{self.s.puuid}") or {}
        pid = me.get("CurrentPartyID")
        p = self.call("GET", f"{self.glz}/parties/v1/parties/{pid}") if pid else None
        members = [m.get("Subject") for m in (p or {}).get("Members") or [] if m.get("Subject")]
        return {m: pid for m in members} if len(members) > 1 else {}

    def in_agent_select(self):
        me = self.call("GET", f"{self.glz}/pregame/v1/players/{self.s.puuid}")
        return bool(me and me.get("MatchID"))

    def dodge(self):
        """Leave agent select. Riot applies the normal dodge penalty (RR loss + queue cooldown)."""
        me = self.call("GET", f"{self.glz}/pregame/v1/players/{self.s.puuid}")
        if not me or not me.get("MatchID"):
            raise AppError("You can only dodge during agent select.")
        try:
            http("POST", f"{self.glz}/pregame/v1/matches/{me['MatchID']}/quit", self.h)
        except HttpError as e:
            raise AppError(f"Riot didn't accept the dodge (HTTP {e.status}).")

    def loadouts(self, phase, mid, players):
        kind = "core-game" if phase == "in game" else "pregame"
        raw = (self.call("GET", f"{self.glz}/{kind}/v1/matches/{mid}/loadouts") or {}).get("Loadouts") or []
        out = {}
        for i, entry in enumerate(raw):  # core-game entries name the player; pregame ones follow player order
            lo = entry.get("Loadout", entry)
            sub = lo.get("Subject") or (players[i]["puuid"] if i < len(players) else None)
            if sub:
                out[sub] = lo.get("Items") or {}
        return out

    def names(self, puuids):
        rows = self.call("PUT", f"{self.pd}/name-service/v2/players", list(puuids)) or []
        return {r["Subject"]: f"{r['GameName']}#{r['TagLine']}" for r in rows if r.get("GameName")}

    # ---- player profiles

    def act(self):
        if self._act is None:
            seasons = (self.call("GET", f"{self.shared}/content-service/v3/content") or {}).get("Seasons") or []
            self._act = next((x["ID"] for x in seasons if x.get("IsActive") and x.get("Type") == "act"), "")
        return self._act

    def mmr(self, puuid):
        """Current rank, RR, peak, wins/games this act."""
        mmr = self.call("GET", f"{self.pd}/mmr/v1/players/{puuid}")
        if mmr is None:
            return None
        seasons = ((mmr.get("QueueSkills") or {}).get("competitive") or {}).get("SeasonalInfoBySeasonID") or {}
        cur = seasons.get(self.act()) or {}
        peak = 0
        for sea in seasons.values():
            peak = max([peak, sea.get("CompetitiveTier") or 0] + [int(t) for t in (sea.get("WinsByTier") or {})])
        return {"tier": cur.get("CompetitiveTier") or 0, "rr": cur.get("RankedRating") or 0,
                "peak": peak, "wins": cur.get("NumberOfWinsWithPlacements") or 0,
                "games": cur.get("NumberOfGames") or 0}

    def history(self, puuid, n, queue=None):
        q = f"&queue={queue}" if queue else ""
        r = self.call("GET", f"{self.pd}/match-history/v1/history/{puuid}?startIndex=0&endIndex={n}{q}") or {}
        return [h["MatchID"] for h in r.get("History") or []]

    def details(self, mid):
        """A finished match, boiled down. Finished matches never change, so they're kept on
        disk: players you've already seen (your duo, today's lobbies) load instantly."""
        if mid in self._details:
            return self._details[mid]
        path = MATCH_CACHE / f"{mid}.json"
        d = None
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            if d.get("v") != MATCH_FORMAT:  # cached before parties were kept: fetch it again
                raise ValueError
        except (OSError, ValueError):
            raw = self.call("GET", f"{self.pd}/match-details/v1/matches/{mid}")
            if raw is None:
                return None
            d = compact_match(raw)  # full replies are ~1-2 MB each in memory
            del raw
            try:
                MATCH_CACHE.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(d, separators=(",", ":")), encoding="utf-8")
            except OSError:
                pass
        if len(self._details) > 80:  # keep memory flat; the disk has the rest
            self._details.pop(next(iter(self._details)))
        self._details[mid] = d
        return d

    def rr_changes(self, puuid):
        r = self.call("GET", f"{self.pd}/mmr/v1/players/{puuid}/competitiveupdates?startIndex=0&endIndex=20") or {}
        return {m["MatchID"]: m.get("RankedRatingEarned") for m in r.get("Matches") or []}


def compact_match(d):
    """Keep only what the tracker reads from a match-details reply; the round-by-round
    data is boiled down to headshot/bodyshot/legshot totals per player."""
    shots = {}
    for rnd in d.get("roundResults") or []:
        for ps in rnd.get("playerStats") or []:
            s = shots.setdefault(ps.get("subject"), [0, 0, 0])
            for dmg in ps.get("damage") or []:
                s[0] += dmg.get("headshots", 0)
                s[1] += dmg.get("bodyshots", 0)
                s[2] += dmg.get("legshots", 0)
    mi = d.get("matchInfo") or {}
    return {"v": MATCH_FORMAT,
            "matchInfo": {k: mi.get(k) for k in ("mapId", "queueID", "gameStartMillis")},
            "players": [{"subject": p.get("subject"), "teamId": p.get("teamId"), "characterId": p.get("characterId"),
                         "partyId": p.get("partyId"),
                         "stats": {k: (p.get("stats") or {}).get(k, 0)
                                   for k in ("kills", "deaths", "assists", "score", "roundsPlayed")}}
                        for p in d.get("players") or []],
            "teams": [{k: t.get(k) for k in ("teamId", "won", "roundsWon")} for t in d.get("teams") or []],
            "shots": shots}


def player_line(d, puuid):
    """One player's stats in a finished match: k, d, a, acs, hs%, won, score, agent, map, queue."""
    p = next((x for x in d.get("players") or [] if x.get("subject") == puuid), None)
    if not p:
        return None
    st = p.get("stats") or {}
    head_, body, legs = (d.get("shots") or {}).get(puuid) or (0, 0, 0)
    teams = {t["teamId"]: t for t in d.get("teams") or []}
    mine = teams.get(p.get("teamId")) or {}
    other = next((t for tid, t in teams.items() if tid != p.get("teamId")), {})
    rounds = st.get("roundsPlayed") or 0
    mi = d.get("matchInfo") or {}
    return {"k": st.get("kills", 0), "d": st.get("deaths", 0), "a": st.get("assists", 0),
            "acs": round(st.get("score", 0) / rounds) if rounds else 0,
            "hs": round(100 * head_ / (head_ + body + legs)) if head_ + body + legs else None,
            "won": mine.get("won"), "score": f"{mine.get('roundsWon', '?')}-{other.get('roundsWon', '?')}",
            "agent": agents().get((p.get("characterId") or "").lower(), "?"),
            "map": maps().get(mi.get("mapId"), "?"), "queue": mi.get("queueID") or "",
            "start": mi.get("gameStartMillis")}


def profile(api: Api, puuid):
    """Rank + recent competitive form for one player."""
    return {"mmr": api.mmr(puuid), **recent_stats(api, puuid)}


def recent_stats(api: Api, puuid):
    """K/D, HS%, W/L and who they queued with, from a player's last few competitive games."""
    out = {"kd": None, "hs": None, "form": "", "party_mates": set(), "stats_done": True}
    k = dth = hs_sum = hs_n = 0
    for mid in api.history(puuid, RECENT_MATCHES, "competitive"):
        d = api.details(mid)
        line = player_line(d, puuid) if d else None
        if not line:
            continue
        k += line["k"]
        dth += line["d"]
        if line["hs"] is not None:
            hs_sum += line["hs"]
            hs_n += 1
        out["form"] += "W" if line["won"] else "L"
        # match details list every player's party, so this is who they actually queued with
        party = next((x.get("partyId") for x in d["players"] if x.get("subject") == puuid), None)
        if party:
            out["party_mates"] |= {x["subject"] for x in d["players"] if x.get("partyId") == party} - {puuid}
    if k or dth:
        out["kd"] = round(k / max(dth, 1), 2)
    if hs_n:
        out["hs"] = round(hs_sum / hs_n)
    return out


# --------------------------------------------------------------------------- parties

def my_loop_state(puuid):
    """"MENUS", "PREGAME" or "INGAME" from the Riot Client on this PC (a ~10 ms local call,
    no internet), or None if it doesn't say."""
    port, password = read_lockfile()
    auth = {"Authorization": "Basic " + base64.b64encode(f"riot:{password}".encode()).decode()}
    try:
        pres = (http("GET", f"https://127.0.0.1:{port}/chat/v4/presences", auth, local=True) or {}).get("presences", [])
    except AppError:
        return None
    for p in pres:
        if p.get("puuid") == puuid and p.get("product") == "valorant" and p.get("private"):
            try:
                priv = json.loads(base64.b64decode(p["private"]))
            except Exception:
                return None
            return priv.get("sessionLoopState") or (priv.get("matchPresenceData") or {}).get("sessionLoopState")
    return None


def party_ids():
    """puuid -> partyId from the Riot Client's chat presences (covers everyone in your match)."""
    port, password = read_lockfile()
    auth = {"Authorization": "Basic " + base64.b64encode(f"riot:{password}".encode()).decode()}
    try:
        pres = (http("GET", f"https://127.0.0.1:{port}/chat/v4/presences", auth, local=True) or {}).get("presences", [])
    except AppError:
        return {}
    out = {}
    for p in pres:
        if p.get("product") != "valorant" or not p.get("private"):
            continue
        try:
            priv = json.loads(base64.b64decode(p["private"]))
        except Exception:
            continue
        pid = priv.get("partyId") or (priv.get("partyPresenceData") or {}).get("partyId")
        if pid:
            out[p["puuid"]] = pid
    return out


def find_groups(players, parties, cache):
    """puuid -> (group number, size). Teammates are linked when the Riot Client reports them in
    the same party now (you and your friends), or when they were in the same party in one of
    their last few games (everyone else: Riot's match details list each player's party)."""
    root = {p["puuid"]: p["puuid"] for p in players}

    def find(x):
        while root[x] != x:
            root[x] = root[root[x]]
            x = root[x]
        return x

    for i, a in enumerate(players):
        for b in players[i + 1:]:
            if a["team"] != b["team"]:
                continue
            pa, pb = a["puuid"], b["puuid"]
            now_a, now_b = parties.get(pa), parties.get(pb)
            if now_a and now_b:  # the Riot Client knows both: trust it over old games
                linked = now_a == now_b
            else:
                linked = (pb in (cache.get(pa) or {}).get("party_mates", ())
                          or pa in (cache.get(pb) or {}).get("party_mates", ()))
            if linked:
                root[find(pa)] = find(pb)
    members = {}
    for p in players:
        members.setdefault(find(p["puuid"]), []).append(p["puuid"])
    out = {}
    for n, (r, ms) in enumerate(sorted((r, ms) for r, ms in members.items() if len(ms) > 1)):
        for m in ms:
            out[m] = (n, len(ms))
    return out


def skins_of(items):
    names, out = skins(), []
    for weapon, wid in SHOWN_WEAPONS:
        sid = (((((items or {}).get(wid) or {}).get("Sockets") or {}).get(SKIN_SOCKET) or {})
               .get("Item") or {}).get("ID", "").lower()
        name = names.get(sid)
        if name and (name.startswith("Standard") or name in ("Melee", "Random Favorite Skin")):
            name = None
        out.append(f"{weapon}: {name or 'default'}")
    return "  |  ".join(out)


# --------------------------------------------------------------------------- data for the window

def _rank(tier, rr=None):
    return {"tier": tier or 0, "name": tier_name(tier), "rr": rr,
            "icon": tier_icons().get(tier) if tier and tier > 2 else tier_icons().get(0)}


def skin_list(items):
    names, icons, out = skins(), skin_icons(), []
    for weapon, wid in SHOWN_WEAPONS:
        sid = (((((items or {}).get(wid) or {}).get("Sockets") or {}).get(SKIN_SOCKET) or {})
               .get("Item") or {}).get("ID", "").lower()
        name = names.get(sid)
        default = not name or name.startswith("Standard") or name in ("Melee", "Random Favorite Skin")
        out.append({"weapon": weapon, "name": "Default" if default else name,
                    "icon": None if default else icons.get(sid)})
    return out


def board(api: Api, snap, cache):
    """Everything the window shows for a match, as plain JSON-able data."""
    me = api.s.puuid
    players, parties = snap["players"], snap["parties"]
    groups = find_groups(players, parties, cache)
    my_team = next((p["team"] for p in players if p["puuid"] == me), None)
    teams, renumber = [], {}
    for team in sorted({p["team"] for p in players}, key=lambda t: t != my_team):
        rows = []
        for p in players:
            if p["team"] != team:
                continue
            pu = p["puuid"]
            prof = cache.get(pu) or {}
            m = prof.get("mmr")
            agent = agents().get(p["agent"]) if p["agent"] else None
            hidden = p["incognito"] and pu != me
            name = (agent or "Hidden") if hidden else snap["names"].get(pu, "Unknown")
            g = groups.get(pu)
            rows.append({
                "puuid": pu, "me": pu == me, "name": name, "hidden": hidden,
                "agent": agent or "Picking...", "agent_icon": agent_icons().get(p["agent"]) if agent else None,
                "level": p["level"], "loading": "mmr" not in prof, "stats_loading": not prof.get("stats_done"),
                "rank": _rank(m["tier"], m["rr"]) if m else None,
                "peak": _rank(m["peak"]) if m else None,
                "wr": round(100 * m["wins"] / m["games"]) if m and m["games"] else None,
                "games": m["games"] if m else None,
                "kd": prof.get("kd"), "hs": prof.get("hs"), "form": prof.get("form", ""),
                "party": {"n": g[0] + 1, "size": g[1], "label": PARTY_WORD.get(g[1], "party")} if g else None,
                "solo": pu in parties and not g,
                "skins": skin_list(snap["loadouts"][pu]) if snap["loadouts"].get(pu) else [],
            })
        rows.sort(key=lambda r: (r["party"] is None, (r["party"] or {}).get("n", 0),
                                 -((r["rank"] or {}).get("tier") or 0)))
        for r in rows:  # number parties in the order they appear: your team first, top to bottom
            if r["party"]:
                old = r["party"]["n"]
                if old not in renumber:
                    renumber[old] = len(renumber) + 1
                r["party"] = {**r["party"], "n": renumber[old]}
        tiers_known = [r["rank"]["tier"] for r in rows if r["rank"] and r["rank"]["tier"] > 2]
        teams.append({"mine": team == my_team, "label": "Your team" if team == my_team else "Enemy team",
                      "avg_rank": _rank(round(sum(tiers_known) / len(tiers_known))) if tiers_known else None,
                      "players": rows})
    return {"phase": snap["phase"], "mid": snap["mid"], "map": snap["map"],
            "map_image": map_images().get(api.map_id), "queue": snap["queue"],
            "queue_name": QUEUE_NAMES.get(snap["queue"], snap["queue"] or "Custom"),
            "can_dodge": snap["phase"] == "agent select", "teams": teams}


def history_data(api: Api, n=10):
    s = api.s
    mids = api.history(s.puuid, max(1, min(n, 20)))
    rr = api.rr_changes(s.puuid)
    with ThreadPoolExecutor(max_workers=2) as pool:
        lines = list(pool.map(lambda m: player_line(api.details(m) or {}, s.puuid), mids))
    m = api.mmr(s.puuid)
    matches = []
    for mid, ln in zip(mids, lines):
        if not ln:
            continue
        a, b = (ln["score"].split("-") + ["", ""])[:2]
        matches.append({**ln, "id": mid,
                        "result": "win" if ln["won"] else "draw" if a == b and ln["queue"] != "deathmatch" else "loss",
                        "queue_name": QUEUE_NAMES.get(ln["queue"], ln["queue"]),
                        "rr": rr.get(mid) if ln["queue"] == "competitive" else None,
                        "map_image": map_images().get(next((k for k, v in maps().items() if v == ln["map"]), None)),
                        "agent_icon": next((agent_icons().get(k) for k, v in agents().items() if v == ln["agent"]), None),
                        "acs": ln["acs"] if ln["queue"] != "deathmatch" else None,
                        "score": ln["score"] if ln["queue"] != "deathmatch" else None})
    return {"account": s.display,
            "rank": _rank(m["tier"], m["rr"]) if m else None, "peak": _rank(m["peak"]) if m else None,
            "wr": round(100 * m["wins"] / m["games"]) if m and m["games"] else None,
            "games": m["games"] if m else 0, "matches": matches}


# --------------------------------------------------------------------------- live screen

RANK_COLORS = [(3, "90"), (6, "33"), (9, "37"), (12, "93"), (15, "96"), (18, "94"), (21, "95"),
               (24, "32"), (27, "31"), (99, "1;93")]


def rank_color(tier):
    if not tier or tier < 3:
        return "90"
    return next(c for top, c in RANK_COLORS if tier < top + 3)


def snapshot(api: Api, cache, load_profiles=True):
    """Gather everything for the current match. `cache` keeps player profiles between refreshes;
    with load_profiles=False the caller fills it (the window does this in the background)."""
    phase, mid, map_name, queue, players = api.match()
    if not phase:
        return None
    puuids = [p["puuid"] for p in players]
    todo = [pu for pu in puuids if pu not in cache]
    if load_profiles and todo:
        with ThreadPoolExecutor(max_workers=5) as pool:
            for pu, prof in zip(todo, pool.map(lambda pu: profile(api, pu), todo)):
                cache[pu] = prof
    return {"phase": phase, "mid": mid, "map": map_name, "queue": queue, "players": players,
            "names": api.names(puuids), "parties": {**party_ids(), **api.my_party()},
            "loadouts": api.loadouts(phase, mid, players)}


def render(api: Api, snap, cache, show_skins=True):
    me = api.s.puuid
    players, parties = snap["players"], snap["parties"]
    groups = find_groups(players, parties, cache)

    print()
    head(f"  {snap['map']}  ·  {QUEUE_NAMES.get(snap['queue'], snap['queue'] or 'Custom')}  ·  {snap['phase']}")
    my_team = next((p["team"] for p in players if p["puuid"] == me), None)
    hdr = f"    {'AGENT':<10} {'PLAYER':<24} {'RANK':<20} {'PEAK':<13} {'WR':>4} {'K/D':>5} {'HS':>4} {'LVL':>4}  PARTY"
    for team in sorted({p["team"] for p in players}, key=lambda t: t != my_team):
        print()
        print(paint(f"  {'YOUR TEAM' if team == my_team else 'ENEMY TEAM'}", "1;32" if team == my_team else "1;31"))
        print(dim(hdr))
        members = [p for p in players if p["team"] == team]
        members.sort(key=lambda p: (p["puuid"] not in groups, (groups.get(p["puuid"]) or (0,))[0],
                                    -((cache.get(p["puuid"]) or {}).get("mmr") or {}).get("tier", 0)))
        for p in members:
            pu = p["puuid"]
            prof = cache.get(pu) or {}
            m = prof.get("mmr")
            agent = agents().get(p["agent"], "?") if p["agent"] else "(picking)"
            if p["incognito"] and pu != me:
                who = agent  # hidden-name players show as their agent, like the in-game scoreboard
            else:
                who = snap["names"].get(pu, pu[:8])
            who = who[:18] + " (you)" if pu == me else who[:24]
            if m:
                rank = f"{tier_name(m['tier'])} {m['rr']}rr" if m["tier"] > 2 else "Unranked"
                peak = tier_name(m["peak"])
                wr = f"{round(100 * m['wins'] / m['games'])}%" if m["games"] else "-"
            else:
                rank, peak, wr = "?", "?", "-"
            kd = f"{prof['kd']:.2f}" if prof.get("kd") is not None else "-"
            hs = f"{prof['hs']}%" if prof.get("hs") is not None else "-"
            lvl = str(p["level"]) if p["level"] else "-"
            if pu in groups:
                n, size = groups[pu]
                tag = paint(f"{PARTY_WORD.get(size, 'party')} {n + 1}",
                            PARTY_COLORS[n % len(PARTY_COLORS)])
            else:
                tag = dim("solo" if pu in parties else "-")
            name_col = paint(f"{who:<24}", "1;32") if pu == me else f"{who:<24}"
            print(f"    {agent:<10} {name_col} "
                  f"{paint(f'{rank:<20}', rank_color(m['tier'] if m else 0))} "
                  f"{paint(f'{peak:<13}', rank_color(m['peak'] if m else 0))} "
                  f"{wr:>4} {kd:>5} {hs:>4} {lvl:>4}  {tag}")
            if show_skins and snap["loadouts"].get(pu):
                print(dim(f"    {'':<10} {skins_of(snap['loadouts'][pu])}"))
    print()
    print(dim(f"  WR = win rate this act · K/D and HS% from the last {RECENT_MATCHES} comp games"))
    print(dim("  party = in a party now (you and friends), or queued together in their recent games"))
    if snap["phase"] == "agent select":
        print(dim("  The enemy team shows up once the match loads."))


def cmd_live(a):
    """Keep a live board of your match, refreshing by itself. Any key returns to the menu."""
    s = get_session()
    api = Api(s)
    show_skins = not getattr(a, "no_skins", False)
    interval = getattr(a, "interval", None) or 5
    cache, shown, last_mid = {}, None, None  # cache: puuid -> profile for this match
    keys = None
    if os.name == "nt" and sys.stdin.isatty():
        import msvcrt
        keys = msvcrt

    def wait(seconds):
        end = time.time() + seconds
        while time.time() < end:
            if keys and keys.kbhit():
                keys.getwch()
                return True
            time.sleep(0.1)
        return False

    while True:
        try:
            snap = snapshot(api, cache)
        except AppError as e:
            clear()
            warn(str(e))
            snap = None
        mid = snap["mid"] if snap else None
        if mid and last_mid and mid != last_mid:
            cache.clear()  # new match: ranks may have moved (agent select -> game keeps the same id)
        # redraw only when something visible changed (phase, agents, loadouts, parties)
        sig = json.dumps(snap, sort_keys=True, default=str) if snap else "menus"
        if sig != shown:
            clear()
            if snap:
                render(api, snap, cache, show_skins)
            else:
                print()
                head("  Live match tracker")
                print()
                info("Waiting for a match... it shows up here as soon as you hit agent select.")
            print(dim("\n  refreshes by itself · press any key to go back"))
            shown = sig
        last_mid = mid
        if not keys:
            return  # not a real console: print once and stop
        if wait(interval):
            return


# --------------------------------------------------------------------------- your match history

def cmd_history(a):
    s = get_session()
    api = Api(s)
    n = max(1, min(int(getattr(a, "count", None) or 10), 20))
    mids = api.history(s.puuid, n)
    if not mids:
        raise AppError("Riot didn't return any recent matches for this account.")
    rr = api.rr_changes(s.puuid)
    with ThreadPoolExecutor(max_workers=5) as pool:
        lines = list(pool.map(lambda m: player_line(api.details(m) or {}, s.puuid), mids))

    m = api.mmr(s.puuid)
    print()
    head(f"  {s.display}")
    if m:
        wr = f"{round(100 * m['wins'] / m['games'])}% WR ({m['games']} games)" if m["games"] else "no games this act"
        rank = f"{tier_name(m['tier'])} {m['rr']}rr" if m["tier"] > 2 else "Unranked"
        print(f"  {paint(rank, rank_color(m['tier']))}   peak {paint(tier_name(m['peak']), rank_color(m['peak']))}   {wr}")
    print()
    print(dim(f"    {'WHEN':<12} {'MODE':<8} {'MAP':<10} {'AGENT':<10} {'RESULT':<8} {'K/D/A':<10} {'ACS':>4} {'HS':>4}  RR"))
    tot = {"k": 0, "d": 0, "wins": 0, "games": 0}
    for mid, ln in zip(mids, lines):
        if not ln:
            print(dim("    (couldn't load this match)"))
            continue
        when = datetime.fromtimestamp(ln["start"] / 1000).strftime("%b %d %H:%M") if ln["start"] else "?"
        draw = not ln["won"] and ln["score"].split("-")[0] == ln["score"].split("-")[-1]
        letter, col = ("W", "32") if ln["won"] else ("D", "33") if draw else ("L", "31")
        res = paint(f"{letter} {ln['score'] if ln['queue'] != 'deathmatch' else '':<6}", col)
        acs = str(ln["acs"]) if ln["queue"] != "deathmatch" else "-"
        kda = f"{ln['k']}/{ln['d']}/{ln['a']}"
        hs = f"{ln['hs']}%" if ln["hs"] is not None else "-"
        change = rr.get(mid)
        rr_txt = (paint(f"{change:+d}", "32" if change > 0 else "31" if change < 0 else "90")
                  if isinstance(change, int) and ln["queue"] == "competitive" else "")
        print(f"    {when:<12} {QUEUE_NAMES.get(ln['queue'], ln['queue']):<8} {ln['map']:<10} {ln['agent']:<10} "
              f"{res} {kda:<10} {acs:>4} {hs:>4}  {rr_txt}")
        tot["k"] += ln["k"]
        tot["d"] += ln["d"]
        tot["games"] += 1
        tot["wins"] += 1 if ln["won"] else 0
    if tot["games"]:
        print()
        print(f"  Last {tot['games']}: {tot['wins']}W {tot['games'] - tot['wins']}L · "
              f"K/D {tot['k'] / max(tot['d'], 1):.2f}")
