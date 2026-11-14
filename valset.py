#!/usr/bin/env python3
"""
valset - save and restore VALORANT settings per Riot account.

Run with no arguments for the interactive shell, or call a command directly:

    python valset.py status
    python valset.py save "main settings"
    python valset.py list
    python valset.py restore 1 --only sens keybinds

Settings live in two places and valset saves both:
  * Riot's cloud (Ares.PlayerSettings): sens, keybinds, crosshair, audio, general.
    Read/written on Riot's player-preferences service with the login tokens the
    Riot Client already holds. Only the Riot Client needs to be running.
  * GameUserSettings.ini: video (resolution, window mode, graphics quality).
Restore with VALORANT closed; the next launch loads the restored settings.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
import re
import shlex
import shutil
import ssl
import stat
import subprocess
import sys
import threading
import time
import http.client as _hc  # aliased: this file has its own http() function
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Run as a script this module is __main__; make `import valset` (in app, tracker, tui, updater)
# return this same module, not a second copy whose AppError is a different class.
sys.modules.setdefault("valset", sys.modules[__name__])

# --------------------------------------------------------------------------- paths & constants

# as an exe, keep saves next to valset.exe (not in PyInstaller's temp folder)
APP_DIR = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
PROFILES = APP_DIR / "profiles"
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
LOCKFILE = LOCALAPPDATA / "Riot Games" / "Riot Client" / "Config" / "lockfile"
VAL_CONFIG = LOCALAPPDATA / "VALORANT" / "Saved" / "Config"
VAL_LOG = LOCALAPPDATA / "VALORANT" / "Saved" / "Logs" / "ShooterGame.log"
GAME_EXE = "VALORANT-Win64-Shipping.exe"

VERSION = "0.2.0-beta"  # must match the GitHub release tag (v0.2.0-beta); the updater compares against it
SETTINGS_TYPE = "Ares.PlayerSettings"
USER_AGENT = f"valset/{VERSION}"  # honest app name; Python's default UA is blocked by Cloudflare
# base64 of {"platformType":"PC","platformOS":"Windows",...} exactly as the game sends it
PLATFORM = ("ew0KCSJwbGF0Zm9ybVR5cGUiOiAiUEMiLA0KCSJwbGF0Zm9ybU9TIjogIldpbmRvd3MiLA0KCSJwbGF0Zm9ybU9TVmVyc2lvbiI6"
            "ICIxMC4wLjE5MDQyLjEuMjU2LjY0Yml0IiwNCgkicGxhdGZvcm1DaGlwc2V0IjogIlVua25vd24iDQp9")
# Riot affinity / region code -> player-preferences host prefix
PP_PREFIX = {
    "eu": "euc1", "eu1": "euc1", "euc1": "euc1", "tr": "euc1", "tr1": "euc1",
    "na": "usw2", "na1": "usw2", "us": "usw2", "usw2": "usw2", "latam": "usw2", "br": "usw2",
    "ap": "apse1", "ap1": "apse1", "apse1": "apse1", "sea": "apse1", "kr": "apse1", "kr1": "apse1",
}
# Per-PC / per-account entries that must stay the target account's own on restore
MACHINE_EXACT = {"VoiceDeviceCaptureHandle", "VoiceDeviceRenderHandle", "DefaultMonitorDeviceID",
                 "PreferredMonitorDeviceID", "PreferredGamePods"}
MACHINE_PREFIX = ("HasSeen", "HasAccepted", "HasEver", "LastSeen")
VIDEO_FILE = "GameUserSettings.ini"

CATEGORIES = ["sens", "crosshair", "keybinds", "audio", "general", "video"]
CLOUD_CATEGORIES = [c for c in CATEGORIES if c != "video"]
AUTO_KEEP = 10  # automatic snapshots kept per account

# --------------------------------------------------------------------------- output helpers

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
if os.name == "nt":
    os.system("")  # turns on ANSI colours in the Windows console
COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def paint(text, code):
    return f"\033[{code}m{text}\033[0m" if COLOR else str(text)


def ok(msg):   print(paint("[ok] ", "32") + msg)
def warn(msg): print(paint("[!]  ", "33") + msg)
def info(msg): print(paint("     ", "0") + msg)
def head(msg): print(paint(msg, "1;36"))
def dim(msg):  return paint(msg, "90")


def confirm(question):
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


class AppError(Exception):
    pass


class HttpError(AppError):
    def __init__(self, msg, status):
        super().__init__(msg)
        self.status = status


# --------------------------------------------------------------------------- HTTP

_SSL = {}
_conns = threading.local()  # per thread: (host, port, local) -> open keep-alive connection


def _ssl_context(local):
    """Built once: creating a context loads the whole Windows certificate store."""
    if local not in _SSL:
        ctx = ssl.create_default_context()
        if local:  # Riot Client uses a self-signed cert on 127.0.0.1 only
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        _SSL[local] = ctx
    return _SSL[local]


def http(method, url, headers=None, body=None, local=False, raw_text=False, _redirects=2):
    """Small JSON-over-HTTPS client. Connections are kept open and reused per host, so
    repeated calls skip the TCP + TLS handshake (much faster, much less CPU)."""
    u = urllib.parse.urlsplit(url)
    host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"User-Agent": USER_AGENT, "Connection": "keep-alive", **(headers or {})}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    pool = getattr(_conns, "pool", None)
    if pool is None:
        pool = _conns.pool = {}
    key = (u.scheme, host, port, local)
    for attempt in (1, 2):  # a kept-alive connection the server already closed fails once: reconnect
        conn = pool.get(key)
        fresh = conn is None
        if fresh:
            conn = (_hc.HTTPSConnection(host, port, timeout=15, context=_ssl_context(local))
                    if u.scheme == "https" else _hc.HTTPConnection(host, port, timeout=15))
            pool[key] = conn
        try:
            conn.request(method, path, body=data, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            break
        except (_hc.HTTPException, OSError) as e:
            conn.close()
            pool.pop(key, None)
            if fresh or attempt == 2:
                raise AppError(f"couldn't reach {url}: {e}") from None
    if resp.will_close:
        conn.close()
        pool.pop(key, None)
    if resp.status in (301, 302, 303, 307, 308) and resp.getheader("Location") and _redirects:
        return http(method, urllib.parse.urljoin(url, resp.getheader("Location")), headers, body, local, raw_text,
                    _redirects - 1)
    if resp.status >= 400:
        detail = raw[:300].decode(errors="replace")
        raise HttpError(f"{method} {url} -> HTTP {resp.status} {detail}", resp.status)
    if raw_text:
        return raw.decode(errors="replace")
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------------------- Riot Client session

@dataclass
class Session:
    puuid: str
    name: str | None
    tag: str | None
    region: str | None
    port: str
    access_token: str
    entitlements: str
    pp_host: str

    @property
    def display(self):
        return f"{self.name}#{self.tag}" if self.name else self.puuid[:8]


def resolve_pp_host(access_token, fallback_region):
    """Riot's affinity service says which settings cluster the account lives on."""
    try:
        jwt = http("GET", "https://riot-geo.pas.si.riotgames.com/pas/v1/product/valorant",
                   {"Authorization": f"Bearer {access_token}"}, raw_text=True).strip()
        payload = jwt.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        affinity = json.loads(base64.urlsafe_b64decode(payload)).get("affinity")
    except Exception:
        affinity = None
    code = (affinity or fallback_region or "na").lower()
    return f"{PP_PREFIX.get(code, code)}.pp.sgp.pvp.net"


def read_lockfile():
    if not LOCKFILE.exists():
        raise AppError("Riot Client isn't running. Open it, log in, then try again.")
    try:
        raw = LOCKFILE.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise AppError(f"couldn't read the Riot lockfile: {e}")
    parts = raw.split(":")  # name:pid:port:password:protocol
    if len(parts) < 5:
        raise AppError("Riot lockfile looks malformed.")
    return parts[-3], parts[-2]


def get_session():
    port, password = read_lockfile()
    base = f"https://127.0.0.1:{port}"
    auth = {"Authorization": "Basic " + base64.b64encode(f"riot:{password}".encode()).decode()}

    def local(path):
        return http("GET", base + path, auth, local=True)

    try:
        ent = local("/entitlements/v1/token")
    except HttpError:
        if game_running():
            raise AppError("VALORANT is open, but the Riot Client lost its sign-in (it does this after "
                           "running for a long time). Open the Riot Client window and sign in again, "
                           "or quit it from the tray and reopen it.")
        raise AppError("Riot Client is open but no account is logged in.")
    except AppError:
        raise AppError("Riot Client isn't responding (still starting, or it closed).")
    if not ent or not ent.get("subject") or not ent.get("accessToken"):
        raise AppError("Riot Client is open but no account is logged in.")

    name = tag = None
    for path in ("/player-account/aliases/v1/active", "/chat/v1/session"):
        try:
            d = local(path) or {}
        except AppError:
            continue
        name = d.get("game_name") or None
        tag = d.get("tag_line") or d.get("game_tag") or None
        if name:
            break

    # The VALORANT config folder name ends in the game region (e.g. "-eu"); the Riot
    # Client's own region (e.g. ME1) is the account's home and can differ.
    region = None
    dirs = account_config_dirs(ent["subject"])
    if dirs and "-" in dirs[0].name:
        region = dirs[0].name.rsplit("-", 1)[-1]
    if not region:
        try:
            region = (local("/riotclient/region-locale") or {}).get("region")
        except AppError:
            pass

    pp_host = resolve_pp_host(ent["accessToken"], region)
    return Session(ent["subject"], name, tag, region, port, ent["accessToken"], ent["token"], pp_host)


_client_version = None


def client_version():
    """VALORANT client version, taken from the game log or a public version API."""
    global _client_version
    if _client_version:
        return _client_version
    try:
        with open(VAL_LOG, "r", encoding="utf-8", errors="ignore") as f:
            hits = re.findall(r"CI server version: (release-\d+\.\d+-shipping-\d+-\d+)", f.read())
            if hits:
                _client_version = hits[-1]  # newest line; a stale version makes Riot return 404
    except OSError:
        pass
    return _client_version or "unknown"


_game_handle = None


def game_running():
    """Is VALORANT open? Once found, the game's process handle is kept and asked directly
    (practically free); only while it's closed is the process list walked."""
    global _game_handle
    if os.name == "nt":
        try:
            k32 = ctypes.windll.kernel32
            if _game_handle:
                if k32.WaitForSingleObject(_game_handle, 0) == 0x102:  # WAIT_TIMEOUT: still running
                    return True
                k32.CloseHandle(_game_handle)
                _game_handle = None
            pid = _process_exists(GAME_EXE)
            if pid:
                _game_handle = k32.OpenProcess(0x00100000, False, pid) or None  # SYNCHRONIZE
            return bool(pid)
        except Exception:
            pass
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {GAME_EXE}", "/NH", "/FO", "CSV"],  # CSV: table mode truncates the name
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return False
    return GAME_EXE.lower() in out.lower()


def _process_exists(exe_name):
    """PID of the first process with this exe name, or 0."""

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if snap in (None, wintypes.HANDLE(-1).value):
        raise OSError("snapshot failed")
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        ok_ = k32.Process32FirstW(snap, ctypes.byref(e))
        target = exe_name.lower()
        while ok_:
            if e.szExeFile.lower() == target:
                return e.th32ProcessID
            ok_ = k32.Process32NextW(snap, ctypes.byref(e))
        return 0
    finally:
        k32.CloseHandle(snap)


# --------------------------------------------------------------------------- cloud settings

def _pp_headers(s: Session):
    return {
        "Authorization": f"Bearer {s.access_token}",
        "X-Riot-Entitlements-JWT": s.entitlements,
        "X-Riot-ClientPlatform": PLATFORM,
        "X-Riot-ClientVersion": client_version(),
    }


def decode_blob(b64):
    return json.loads(zlib.decompress(base64.b64decode(b64), -15))


def encode_blob(obj):
    comp = zlib.compressobj(wbits=-15)
    raw = comp.compress(json.dumps(obj, separators=(",", ":")).encode()) + comp.flush()
    return base64.b64encode(raw).decode()


def _pp_call(s: Session, method, path, body=None):
    try:
        return http(method, f"https://{s.pp_host}/playerPref/v3/{path}", _pp_headers(s), body)
    except HttpError as e:
        if e.status in (401, 403):
            raise AppError(f"Riot refused the request (HTTP {e.status}). Restart the Riot Client and try again.")
        if e.status == 404:
            raise AppError("Riot returned 404. Usually the game version is stale: launch VALORANT once "
                           "after a patch so the log has the new version, then retry.")
        raise


def get_cloud(s: Session):
    r = _pp_call(s, "GET", f"getPreference/{SETTINGS_TYPE}")
    if not r or not r.get("data"):
        raise AppError("Riot has no settings stored for this account yet. Launch VALORANT on it once.")
    return decode_blob(r["data"])


def put_cloud(s: Session, settings):
    _pp_call(s, "PUT", "savePreference", {"type": SETTINGS_TYPE, "data": encode_blob(settings)})


# --------------------------------------------------------------------------- categorising & merging

AUDIO_WORDS = ("volume", "audio", "sound", "music", "voice", "mute", "hrtf", "speaker", "mic")


def category(setting_enum: str):
    name = setting_enum.split("::")[-1]
    if name in MACHINE_EXACT or name.startswith(MACHINE_PREFIX):
        return "machine"  # never restored: always kept from the target account
    e = name.lower()
    if "crosshair" in e:
        return "crosshair"
    if e.startswith("mic"):
        return "audio"
    if "sensitivity" in e or "mouseinvert" in e or "invertmouse" in e:
        return "sens"
    if any(w in e for w in AUDIO_WORDS):
        return "audio"
    return "general"


def is_setting_list(v):
    return isinstance(v, list) and len(v) > 0 and all(isinstance(i, dict) and "settingEnum" in i for i in v)


def is_mapping_key(key):
    return key.lower().endswith("mappings")


def merge(current, snap, cats):
    """Start from the account's current cloud settings and swap in the chosen categories
    from the snapshot. Keys we don't recognise are left untouched, so settings Riot adds
    in future patches survive a restore."""
    out = json.loads(json.dumps(current))
    for key in set(current) | set(snap):
        cv, sv = current.get(key), snap.get(key)
        if is_setting_list(cv) or is_setting_list(sv):
            cv, sv = cv or [], sv or []
            out[key] = ([i for i in cv if category(i["settingEnum"]) not in cats] +
                        [i for i in sv if category(i["settingEnum"]) in cats])
        elif is_mapping_key(key):
            if "keybinds" in cats and sv is not None:
                out[key] = sv
        elif key not in current and sv is not None and "general" in cats and "version" not in key.lower():
            out[key] = sv
    return out


def flatten(cloud):
    """(category, label) -> printable value, used for show/diff."""
    out = {}
    for key, val in cloud.items():
        if is_setting_list(val):
            for it in val:
                e = it["settingEnum"]
                out[(category(e), e.split("::")[-1])] = it.get("value")
        elif is_mapping_key(key) and isinstance(val, list):
            prefix = "" if key == "actionMappings" else f"[{key[:-8]}] "
            for it in val:
                if not isinstance(it, dict):
                    continue
                label = f"{prefix}{it.get('name', '?')} #{it.get('bindIndex', 0)}"
                if it.get("characterName"):
                    label += f" ({it['characterName']})"
                extras = {k: v for k, v in it.items()
                          if k not in ("name", "bindIndex", "characterName", "key") and v not in (False, None, "", 0)}
                value = str(it.get("key", ""))
                if extras:
                    value += " " + json.dumps(extras, sort_keys=True)
                out[("keybinds", label)] = value
    return out


def short(v, n=60):
    s = json.dumps(v) if not isinstance(v, str) else v
    return s if len(s) <= n else s[: n - 3] + "..."


# --------------------------------------------------------------------------- local (video) config

def account_config_dirs(puuid):
    if not VAL_CONFIG.exists():
        return []
    return sorted(d for d in VAL_CONFIG.iterdir() if d.is_dir() and d.name.lower().startswith(puuid.lower()))


def read_ini_dir(root: Path):
    """(file, section, key) -> value for the video .ini under root."""
    out = {}
    for f in root.rglob(VIDEO_FILE):
        section = ""
        for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
            elif "=" in line and not line.startswith(";"):
                k, v = line.split("=", 1)
                out[(f.relative_to(root).as_posix(), section, k.strip())] = v.strip()
    return out


def save_local_config(puuid, dest: Path):
    saved = []
    for d in account_config_dirs(puuid):
        files = list(d.rglob(VIDEO_FILE))
        for f in files:
            target = dest / d.name / f.relative_to(d)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
        if files:
            saved.append(d.name)
    return saved


def restore_local_config(snap_video: Path, target_puuid):
    sources = [d for d in snap_video.iterdir() if d.is_dir()] if snap_video.exists() else []
    targets = account_config_dirs(target_puuid)
    if not sources:
        warn("This snapshot has no video settings, skipping video.")
        return 0
    if not targets:
        warn("No local config folder for this account yet. Launch VALORANT on it once, then restore video.")
        return 0
    copied = 0
    for tdir in targets:
        suffix = tdir.name[len(target_puuid):]
        src = next((s for s in sources if s.name.endswith(suffix)), sources[0])
        for f in src.rglob(VIDEO_FILE):
            dst = tdir / f.relative_to(src)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                os.chmod(dst, stat.S_IWRITE | stat.S_IREAD)  # some people set these read-only
            shutil.copy2(f, dst)
            copied += 1
    return copied


# --------------------------------------------------------------------------- snapshot store

@dataclass
class Snap:
    path: Path
    meta: dict

    @property
    def id(self): return self.meta["id"]
    @property
    def label(self): return self.meta.get("label") or ""
    @property
    def puuid(self): return self.meta["puuid"]
    @property
    def account(self): return self.meta.get("account") or self.puuid[:8]
    @property
    def video_dir(self): return self.path / "video"

    def cloud(self):
        return json.loads((self.path / "cloud.json").read_text(encoding="utf-8"))


def slug(text):
    return re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-")[:40]


def write_account(s: Session):
    d = PROFILES / s.puuid
    d.mkdir(parents=True, exist_ok=True)
    (d / "account.json").write_text(json.dumps({
        "puuid": s.puuid, "name": s.name, "tag": s.tag, "region": s.region,
        "last_seen": datetime.now().isoformat(timespec="seconds"),
    }, indent=2), encoding="utf-8")


def all_snapshots(current_puuid=None):
    snaps = []
    if PROFILES.exists():
        for meta_file in PROFILES.glob("*/snapshots/*/meta.json"):
            try:
                snaps.append(Snap(meta_file.parent, json.loads(meta_file.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError):
                pass
    # current account first, then the rest grouped by account; newest first inside each group
    snaps.sort(key=lambda x: x.meta.get("created", ""), reverse=True)
    snaps.sort(key=lambda x: (x.puuid != current_puuid, x.account.lower()))
    return snaps


def resolve(ref, current_puuid=None):
    snaps = all_snapshots(current_puuid)
    if not snaps:
        raise AppError("No snapshots saved yet. Use: save [label]")
    if ref.isdigit():
        i = int(ref)
        if 1 <= i <= len(snaps):
            return snaps[i - 1]
        raise AppError(f"No snapshot #{i}. Run 'list' to see the numbers.")
    for test in (lambda x: x.id == ref, lambda x: x.label.lower() == ref.lower()):
        hits = [x for x in snaps if test(x)]
        if hits:
            return hits[0]
    hits = [x for x in snaps if ref.lower() in (x.id + " " + x.label).lower()]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise AppError(f"'{ref}' matches {len(hits)} snapshots. Use the number from 'list'.")
    raise AppError(f"No snapshot matches '{ref}'.")


def save_snapshot(s: Session, label="", auto=False):
    cloud = get_cloud(s)
    now = datetime.now()
    sid = now.strftime("%Y%m%d-%H%M%S") + (f"_{slug(label)}" if slug(label) else "")
    path = PROFILES / s.puuid / "snapshots" / sid
    n = 2
    while path.exists():
        path = path.with_name(f"{sid}-{n}")
        n += 1
    path.mkdir(parents=True)
    (path / "cloud.json").write_text(json.dumps(cloud, indent=2), encoding="utf-8")
    video = save_local_config(s.puuid, path / "video")
    meta = {
        "id": path.name, "label": label, "auto": auto,
        "created": now.isoformat(timespec="seconds"),
        "puuid": s.puuid, "account": s.display, "region": s.region,
        "client_version": client_version(), "video_folders": video,
    }
    (path / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    write_account(s)
    if auto:
        autos = [x for x in all_snapshots(s.puuid) if x.puuid == s.puuid and x.meta.get("auto")]
        for old in autos[AUTO_KEEP:]:
            shutil.rmtree(old.path, ignore_errors=True)
    return Snap(path, meta)


# --------------------------------------------------------------------------- commands

def cmd_status(a):
    head("VALORANT settings - status")
    try:
        s = get_session()
    except AppError as e:
        warn(str(e))
        return
    print(f"  Riot Client : {paint('running', '32')} (port {s.port})")
    print(f"  Account     : {paint(s.display, '1')}  {dim('(' + (s.region or '?') + ')')}")
    print(f"  PUUID       : {dim(s.puuid)}")
    print(f"  VALORANT    : {paint('running', '33') if game_running() else 'not running'}")
    dirs = account_config_dirs(s.puuid)
    print(f"  Video config: {', '.join(d.name for d in dirs) if dirs else paint('not found (launch the game once)', '33')}")
    mine = [x for x in all_snapshots(s.puuid) if x.puuid == s.puuid]
    print(f"  Snapshots   : {len(mine)} saved for this account")


def cmd_save(a):
    s = get_session()
    label = " ".join(a.label).strip()
    snap = save_snapshot(s, label)
    ok(f"Saved snapshot {paint(snap.id, '1')} for {s.display}")
    if not snap.meta["video_folders"]:
        warn("No local video settings found for this account, so only cloud settings were saved.")


def cmd_list(a):
    current = None
    try:
        current = get_session().puuid
    except AppError:
        pass
    snaps = all_snapshots(current)
    if not snaps:
        info("No snapshots yet. Use: save [label]")
        return
    last = None
    for i, x in enumerate(snaps, 1):
        if x.puuid != last:
            tag = paint("  <- logged in", "32") if x.puuid == current else ""
            print()
            head(f"{x.account}{tag}")
            last = x.puuid
        when = x.meta.get("created", "").replace("T", " ")
        label = x.label or dim("(no label)")
        flags = dim(" auto") if x.meta.get("auto") else ""
        video = "" if x.meta.get("video_folders") else dim(" no-video")
        print(f"  {paint(f'{i:>3}', '1;33')}  {when}  {label}{flags}{video}")
    print()


def cmd_show(a):
    snap = resolve(a.snapshot, _current_puuid())
    cloud = snap.cloud()
    flat = flatten(cloud)
    head(f"Save \"{snap.label or snap.id}\"  (from {snap.account})")
    if snap.label:
        info(f"label: {snap.label}")
    info(f"created: {snap.meta.get('created', '?').replace('T', ' ')}   game: {snap.meta.get('client_version', '?')}")
    print()
    counts = {c: 0 for c in CLOUD_CATEGORIES}
    for (cat, _), _v in flat.items():
        if cat in counts:
            counts[cat] += 1
    info("  ".join(f"{c}: {n}" for c, n in counts.items()))
    print()
    head("Sensitivity")
    for (cat, label), v in sorted(flat.items()):
        if cat == "sens":
            print(f"  {label:<45} {short(v)}")
    ini = read_ini_dir(snap.video_dir) if snap.video_dir.exists() else {}
    vals = {k: v for (f, sec, k), v in ini.items()}
    if ini:
        print()
        head("Video")
        rx, ry = vals.get("ResolutionSizeX"), vals.get("ResolutionSizeY")
        mode = {"0": "fullscreen", "1": "windowed fullscreen", "2": "windowed"}.get(vals.get("FullscreenMode", ""), "?")
        print(f"  resolution {rx}x{ry}, {mode}")
    if a.all:
        for c in CLOUD_CATEGORIES:
            if c == "sens":
                continue
            print()
            head(c.capitalize())
            for (cat, label), v in sorted(flat.items()):
                if cat == c:
                    print(f"  {label:<45} {short(v)}")


def cmd_diff(a):
    s = get_session()
    snap = resolve(a.snapshot, s.puuid)
    cur, old = flatten(get_cloud(s)), flatten(snap.cloud())
    head(f"Current settings ({s.display})  vs  snapshot {snap.id} ({snap.account})")
    total = 0
    for c in CLOUD_CATEGORIES:
        rows = []
        for k in sorted(set(cur) | set(old)):
            if k[0] == c and cur.get(k) != old.get(k):
                rows.append((k[1], cur.get(k, "-"), old.get(k, "-")))
        total += len(rows)
        if rows:
            print()
            head(f"{c}  ({len(rows)} different)")
            for label, now, then in rows[:a.max]:
                print(f"  {label:<40} {paint(short(now, 30), '31')}  ->  {paint(short(then, 30), '32')}")
            if len(rows) > a.max:
                info(dim(f"... and {len(rows) - a.max} more"))
    dirs = account_config_dirs(s.puuid)
    if dirs and snap.video_dir.exists():
        src = next((d for d in snap.video_dir.iterdir() if d.is_dir()), None)
        if src:
            now_ini, then_ini = read_ini_dir(dirs[0]), read_ini_dir(src)
            rows = [(k, now_ini.get(k, "-"), then_ini.get(k, "-"))
                    for k in sorted(set(now_ini) | set(then_ini)) if now_ini.get(k) != then_ini.get(k)]
            total += len(rows)
            if rows:
                print()
                head(f"video  ({len(rows)} different)")
                for (f, sec, k), now, then in rows[:a.max]:
                    print(f"  {k:<40} {paint(short(now, 30), '31')}  ->  {paint(short(then, 30), '32')}")
                if len(rows) > a.max:
                    info(dim(f"... and {len(rows) - a.max} more"))
    print()
    if total:
        info(f"{total} differences.  red = now, green = what restoring would set")
    else:
        ok("No differences - this snapshot matches your current settings.")


def parse_cats(values):
    cats = []
    for v in values or []:
        for p in v.split(","):
            p = p.strip().lower()
            if not p:
                continue
            if p in ("sensitivity", "mouse"):
                p = "sens"
            if p in ("keys", "binds"):
                p = "keybinds"
            if p not in CATEGORIES:
                raise AppError(f"Unknown category '{p}'. Use: {', '.join(CATEGORIES)}")
            cats.append(p)
    return cats


def cmd_restore(a):
    s = get_session()
    snap = resolve(a.snapshot, s.puuid)
    cats = parse_cats(a.only) or list(CATEGORIES)
    cats = [c for c in cats if c not in parse_cats(a.skip)]
    if not cats:
        raise AppError("Nothing left to restore after --skip.")
    cloud_cats = [c for c in cats if c != "video"]
    if game_running():
        raise AppError("Close VALORANT first. It overwrites your settings when it exits; "
                       "restore with it closed and the next launch loads them.")

    head("Restore")
    info(f"from : {snap.id}  ({snap.account})")
    info(f"to   : {paint(s.display, '1')}" + (paint("   (different account)", "33") if snap.puuid != s.puuid else ""))
    info(f"parts: {', '.join(cats)}")
    if not a.yes and not confirm("Go ahead?"):
        info("Cancelled.")
        return

    r = apply_snapshot(s, snap, cats, getattr(a, "backup_label", None) or "before restore")
    if r["backup"]:
        ok(f"Backed up current settings as {r['backup']}")
        if r["verified"]:
            ok(f"Settings restored ({', '.join(r['cloud_cats'])}) and verified")
        else:
            warn(f"Settings sent ({', '.join(r['cloud_cats'])}) but Riot's copy doesn't fully match. Run 'diff' to check.")
        info(dim(f"Undo with: restore {r['backup']}"))
    if r["video_files"]:
        ok(f"Video settings restored ({r['video_files']} file{'s' if r['video_files'] != 1 else ''})")
    info("Launch VALORANT and the restored settings load in.")


def apply_snapshot(s: Session, snap: Snap, cats, backup_label="before restore"):
    """Back up the account, then write the chosen parts of `snap` onto it. Shared by the
    terminal and the window. Returns what happened."""
    if game_running():
        raise AppError("Close VALORANT first. It overwrites your settings when it exits.")
    cloud_cats = [c for c in cats if c != "video"]
    out = {"backup": None, "verified": None, "cloud_cats": cloud_cats, "video_files": 0}
    if cloud_cats:
        backup = save_snapshot(s, backup_label, auto=True)
        merged = merge(get_cloud(s), snap.cloud(), set(cloud_cats))
        put_cloud(s, merged)
        out["backup"] = backup.id
        out["verified"] = flatten(get_cloud(s)) == flatten(merged)
    if "video" in cats:
        out["video_files"] = restore_local_config(snap.video_dir, s.puuid)
    return out


def undo_backups(s: Session):
    """Backups made right before a load, newest first. Each one is used up once undone,
    so undoing again steps further back instead of redoing the load."""
    return [x for x in all_snapshots(s.puuid)
            if x.puuid == s.puuid and x.meta.get("auto") and x.label == "before restore"]


def cmd_delete(a):
    snap = resolve(a.snapshot, _current_puuid())
    if not a.yes and not confirm(f"Delete snapshot {snap.id} ({snap.account})?"):
        info("Cancelled.")
        return
    shutil.rmtree(snap.path)
    ok(f"Deleted {snap.id}")


def cmd_export(a):
    snap = resolve(a.snapshot, _current_puuid())
    out = Path(a.file) if a.file else Path.cwd() / f"valset_{slug(snap.account)}_{snap.id}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in snap.path.rglob("*"):
            if f.is_file():
                z.write(f, f.relative_to(snap.path).as_posix())
    ok(f"Exported to {out}")


def cmd_import(a):
    src = Path(a.file)
    if not src.exists():
        raise AppError(f"File not found: {src}")
    with zipfile.ZipFile(src) as z:
        try:
            meta = json.loads(z.read("meta.json"))
        except KeyError:
            raise AppError("That zip isn't a valset export (no meta.json).")
        if "cloud.json" not in z.namelist():
            raise AppError("That zip isn't a valset export (no cloud.json).")
        puuid = slug(meta.get("puuid", "imported")) or "imported"
        base = PROFILES / puuid / "snapshots"
        dest = base / slug(meta.get("id", "import"))
        n = 2
        while dest.exists():
            dest = base / f"{slug(meta.get('id', 'import'))}-{n}"
            n += 1
        dest.mkdir(parents=True)
        root = dest.resolve()
        for name in z.namelist():
            target = (dest / name).resolve()
            if not target.is_relative_to(root):
                raise AppError(f"Refusing unsafe path in zip: {name}")
            if name.endswith("/"):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(name))
        meta["id"] = dest.name
        meta["puuid"] = puuid
        (dest / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    ok(f"Imported {dest.name} ({meta.get('account', '?')}). See 'list'.")


def cmd_watch(a):
    head("Watching for account switches (Ctrl+C to stop)")
    if not a.no_save:
        info(dim("Each account gets one automatic snapshot the first time it's seen."))
    last, saved = None, set()
    try:
        while True:
            try:
                s = get_session()
                if s.puuid != last:
                    ok(f"{datetime.now():%H:%M:%S}  logged in: {s.display}")
                    last = s.puuid
                if not a.no_save and s.puuid not in saved:
                    snap = save_snapshot(s, "auto on login", auto=True)
                    info(f"saved {snap.id}")
                    saved.add(s.puuid)
            except AppError as e:
                if last is not None:
                    warn(f"{datetime.now():%H:%M:%S}  {e}")
                    last = None
            time.sleep(a.interval)
    except KeyboardInterrupt:
        print()
        info("Stopped watching.")


def cmd_open(a):
    PROFILES.mkdir(parents=True, exist_ok=True)
    os.startfile(PROFILES)


def _current_puuid():
    try:
        return get_session().puuid
    except AppError:
        return None


def _live(a):
    import tracker  # imported here: tracker imports from this file
    tracker.cmd_live(a)


def _history(a):
    import tracker
    tracker.cmd_history(a)


def _update(a):
    import updater
    return updater.cmd_update(a)


# --------------------------------------------------------------------------- CLI wiring

def build_parser():
    p = argparse.ArgumentParser(
        prog="valset",
        description="Save and restore VALORANT settings per Riot account.",
        epilog=f"categories: {', '.join(CATEGORIES)}.  <snap> = number from 'list', snapshot id, or label.",
    )
    p.add_argument("--version", action="version", version=f"valset {VERSION}")
    sub = p.add_subparsers(dest="cmd", metavar="command")

    sub.add_parser("status", help="show Riot Client, logged-in account and game state").set_defaults(fn=cmd_status)

    sp = sub.add_parser("save", help="save the current account's settings")
    sp.add_argument("label", nargs="*", help="optional name, e.g. 'main settings'")
    sp.set_defaults(fn=cmd_save)

    sub.add_parser("list", aliases=["ls"], help="list saved snapshots").set_defaults(fn=cmd_list)

    sp = sub.add_parser("show", help="show what's inside a snapshot")
    sp.add_argument("snapshot")
    sp.add_argument("--all", action="store_true", help="print every setting")
    sp.set_defaults(fn=cmd_show)

    sp = sub.add_parser("diff", help="compare a snapshot with your current settings")
    sp.add_argument("snapshot")
    sp.add_argument("--max", type=int, default=15, help="rows per category (default 15)")
    sp.set_defaults(fn=cmd_diff)

    sp = sub.add_parser("restore", help="apply a snapshot to the logged-in account")
    sp.add_argument("snapshot")
    sp.add_argument("--only", nargs="+", metavar="CAT", help="restore just these categories")
    sp.add_argument("--skip", nargs="+", metavar="CAT", help="restore everything except these")
    sp.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    sp.set_defaults(fn=cmd_restore)

    sp = sub.add_parser("delete", aliases=["rm"], help="delete a snapshot")
    sp.add_argument("snapshot")
    sp.add_argument("-y", "--yes", action="store_true")
    sp.set_defaults(fn=cmd_delete)

    sp = sub.add_parser("export", help="pack a snapshot into a .zip to share")
    sp.add_argument("snapshot")
    sp.add_argument("file", nargs="?")
    sp.set_defaults(fn=cmd_export)

    sp = sub.add_parser("import", help="add a snapshot from a .zip")
    sp.add_argument("file")
    sp.set_defaults(fn=cmd_import)

    sp = sub.add_parser("watch", help="detect account switches and auto-save each account")
    sp.add_argument("--no-save", action="store_true", help="just report switches")
    sp.add_argument("--interval", type=float, default=5, help="seconds between checks")
    sp.set_defaults(fn=cmd_watch)

    sp = sub.add_parser("live", aliases=["party"], help="live tracker: ranks, peaks, K/D, parties, skins in your match")
    sp.add_argument("--no-skins", action="store_true", help="hide the skin line under each player")
    sp.add_argument("--interval", type=float, default=5, help="seconds between refreshes")
    sp.set_defaults(fn=_live)

    sp = sub.add_parser("history", help="your recent matches with K/D/A, ACS, HS%% and RR")
    sp.add_argument("count", nargs="?", type=int, default=10, help="how many matches (max 20)")
    sp.set_defaults(fn=_history)

    sp = sub.add_parser("update", help="check GitHub for a newer valset and install it")
    sp.add_argument("--check", action="store_true", help="only say whether there's an update")
    sp.add_argument("-y", "--yes", action="store_true", help="install without asking")
    sp.set_defaults(fn=_update)

    sub.add_parser("menu", help="the simple numbered menu instead of the full valset screen")

    sub.add_parser("open", help="open the snapshots folder in Explorer").set_defaults(fn=cmd_open)
    return p


def run(parser, argv):
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2
    if not getattr(args, "fn", None):
        parser.print_help()
        return 0
    try:
        args.fn(args)
        return 0
    except AppError as e:
        print(paint("[x]  ", "31") + str(e))
        return 1


# --------------------------------------------------------------------------- simple menu (default)

CAT_NAMES = {
    "sens": "Mouse sensitivity",
    "crosshair": "Crosshair",
    "keybinds": "Keybinds",
    "audio": "Audio & voice chat",
    "general": "General / gameplay options",
    "video": "Video & graphics",
}


def ask(prompt):
    try:
        return input(prompt).replace("﻿", "").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def pause():
    ask(dim("\n  Press Enter to go back to the menu..."))


def clear():
    if sys.stdout.isatty():
        os.system("cls" if os.name == "nt" else "clear")


def nice_date(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%b %d, %H:%M")
    except (TypeError, ValueError):
        return "?"


def pick_number(prompt, maximum):
    """Returns 1..maximum, or None for back/cancel."""
    while True:
        c = ask(prompt)
        if c is None or c in ("", "0"):
            return None
        if c.isdigit() and 1 <= int(c) <= maximum:
            return int(c)
        warn(f"Type a number from 1 to {maximum}, or 0 to go back.")


def pick_save(s, title, backups=True):
    snaps = all_snapshots(s.puuid if s else None)
    if not backups:
        snaps = [x for x in snaps if not x.meta.get("auto")]
    snaps.sort(key=lambda x: bool(x.meta.get("auto")))  # your own saves first, backups last
    if not snaps:
        info("You don't have any saves yet. Pick option 1 on the menu first.")
        return None
    print()
    head(f"  {title}")
    print()
    for i, x in enumerate(snaps, 1):
        name = x.label or x.id
        extra = dim("  (automatic backup)") if x.meta.get("auto") else ""
        print(f"  {paint(f'[{i}]', '1;33')} {paint(name, '1')}  {dim('- from ' + x.account + ', ' + nice_date(x.meta.get('created')))}{extra}")
    print(f"  {paint('[0]', '1;33')} Back")
    print()
    n = pick_number("  Pick a number: ", len(snaps))
    return snaps[n - 1] if n else None


def choose_parts():
    chosen = {c: True for c in CATEGORIES}
    while True:
        print()
        head("  What should be loaded?  (type a number to switch it on/off, Enter when done)")
        for i, c in enumerate(CATEGORIES, 1):
            box = paint("[x]", "32") if chosen[c] else "[ ]"
            print(f"  {paint(str(i), '1;33')}. {box} {CAT_NAMES[c]}")
        c = ask("  > ")
        if c is None:
            return None
        if c == "":
            picked = [k for k, v in chosen.items() if v]
            if picked:
                return picked
            warn("Switch on at least one thing.")
        elif c.isdigit() and 1 <= int(c) <= len(CATEGORIES):
            k = CATEGORIES[int(c) - 1]
            chosen[k] = not chosen[k]


def wait_for_game_closed():
    while game_running():
        warn("VALORANT is open. Close the game, then press Enter.  (0 = cancel)")
        if ask("  ") in (None, "0"):
            return False
    return True


def menu_save(s):
    print()
    name = ask(f"  Give this save a name (or just press Enter to call it \"{s.display}\"): ")
    if name is None:
        return
    snap = save_snapshot(s, name or s.display)
    print()
    ok(f"Saved! Your settings are stored as \"{snap.label}\".")
    info("Log into any account and pick option 2 to load them.")


def menu_load(s):
    snap = pick_save(s, "Which save do you want to load onto this account?")
    if not snap:
        return
    print()
    head("  Load everything, or only some parts?")
    print(f"  {paint('[1]', '1;33')} Everything {dim('(recommended)')}")
    print(f"  {paint('[2]', '1;33')} Let me choose (e.g. only crosshair + sensitivity)")
    print(f"  {paint('[0]', '1;33')} Back")
    print()
    n = pick_number("  Pick a number: ", 2)
    if not n:
        return
    parts = list(CATEGORIES) if n == 1 else choose_parts()
    if not parts:
        return
    if not wait_for_game_closed():
        return
    s = get_session()  # whoever is logged in right now is who gets the settings
    print()
    info(f"Load \"{paint(snap.label or snap.id, '1')}\" onto {paint(s.display, '1')}?")
    info(dim("Your current settings are backed up first, so you can undo this."))
    if not confirm("  Go ahead?"):
        info("Cancelled, nothing changed.")
        return
    print()
    cmd_restore(argparse.Namespace(snapshot=snap.id, only=parts, skip=None, yes=True))
    print()
    ok("Done! Start VALORANT and your settings will be there.")


def menu_view(s):
    snap = pick_save(s, "Which save do you want to look at?")
    if not snap:
        return
    print()
    cmd_show(argparse.Namespace(snapshot=snap.id, all=False))
    if s and confirm("\n  Compare it with this account's current settings?"):
        print()
        cmd_diff(argparse.Namespace(snapshot=snap.id, max=15))


def menu_undo(s):
    # Only backups made right before a load count; each one is used up once undone,
    # so pressing Undo again steps further back instead of redoing the load.
    backups = undo_backups(s)
    if not backups:
        info("Nothing to undo on this account.")
        return
    b = backups[0]
    print()
    info(f"This puts {paint(s.display, '1')} back to how it was on {nice_date(b.meta.get('created'))}.")
    if not wait_for_game_closed() or not confirm("  Undo the last load?"):
        return
    print()
    cmd_restore(argparse.Namespace(snapshot=b.id, only=None, skip=None, yes=True, backup_label="before undo"))
    shutil.rmtree(b.path, ignore_errors=True)


def menu_delete(s):
    snap = pick_save(s, "Which save do you want to delete?")
    if snap and confirm(f"  Delete \"{snap.label or snap.id}\" for good?"):
        shutil.rmtree(snap.path)
        ok("Deleted.")


def quick_state():
    """Cheap check of (logged-in puuid, game open) used to refresh the menu live."""
    try:
        port, pw = read_lockfile()
        auth = {"Authorization": "Basic " + base64.b64encode(f"riot:{pw}".encode()).decode()}
        puuid = (http("GET", f"https://127.0.0.1:{port}/entitlements/v1/token", auth, local=True) or {}).get("subject")
    except Exception:
        puuid = None
    return puuid, game_running()


def draw_menu(options):
    clear()
    print(paint("\n  VALORANT SETTINGS SAVER", "1;31"))
    print(dim("  ----------------------------------------"))
    try:
        s = get_session()
        print(f"  Logged in as : {paint(s.display, '1;32')}")
    except AppError as e:
        s = None
        print(f"  Logged in as : {paint('nobody', '33')}  {dim('(' + str(e) + ')')}")
    running = game_running()
    print(f"  VALORANT     : {'open' if running else 'closed'}")
    print(dim("  ----------------------------------------"))
    print(dim("  (updates by itself when you switch accounts)"))
    print()
    for i, (label, _, _) in enumerate(options, 1):
        print(f"  {paint(f'[{i}]', '1;33')} {label}")
    print(f"  {paint('[0]', '1;33')} Exit")
    print()
    print("  Pick a number: ", end="", flush=True)
    return (s.puuid if s else None, running)


def live_prompt(options, every=2.0):
    """Wait for the menu choice, redrawing the menu whenever the logged-in account or
    game state changes. Falls back to plain input() when not in a real console."""
    state = draw_menu(options)
    if os.name != "nt" or not sys.stdin.isatty():
        return ask("")
    import msvcrt
    buf, last = "", time.time()
    while True:
        if msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch in ("\r", "\n"):
                print()
                return buf.strip()
            if ch == "\x03":
                print()
                return None
            if ch in ("\x00", "\xe0"):  # arrow/function keys send two codes
                msvcrt.getwch()
            elif ch == "\x08":
                if buf:
                    buf = buf[:-1]
                    print("\b \b", end="", flush=True)
            elif ch.isprintable():
                buf += ch
                print(ch, end="", flush=True)
            continue
        time.sleep(0.05)
        if not buf and time.time() - last >= every:  # don't redraw while you're typing
            last = time.time()
            if quick_state() != state:
                state = draw_menu(options)


def menu(parser):
    options = [
        ("Save my settings", menu_save, True),
        ("Load a save onto this account", menu_load, True),
        ("Look at my saves", menu_view, False),
        ("Undo the last load", menu_undo, True),
        ("Delete a save", menu_delete, False),
        ("Live match tracker", lambda s: _live(None), True),
        ("My match history", lambda s: _history(None), True),
        ("Check for updates", lambda s: _update(argparse.Namespace(check=False, yes=False)), False),
        ("Advanced (type commands)", None, False),
    ]
    while True:
        c = live_prompt(options)
        if c is None or c == "0":
            return
        if not c.isdigit() or not 1 <= int(c) <= len(options):
            continue
        label, fn, needs_login = options[int(c) - 1]
        if fn is None:
            command_shell(parser)
            continue
        try:  # re-check now: the account may have been switched while the menu sat open
            s = get_session()
        except AppError:
            s = None
        if needs_login and not s:
            print()
            warn("Open the Riot Client and log in first, then try again.")
            pause()
            continue
        try:
            fn(s)
        except AppError as e:
            print()
            print(paint("[x]  ", "31") + str(e))
        pause()


def command_shell(parser):
    clear()
    head("Advanced mode")
    info(dim("Type 'help' for commands, 'back' to return to the menu."))
    print()
    while True:
        try:
            line = input(paint("valset> ", "1;31")).replace("﻿", "").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line.lower() in ("exit", "quit", "q", "back", "menu"):
            break
        if line.lower() in ("help", "?", "h"):
            parser.print_help()
            continue
        if line.lower() in ("cls", "clear"):
            os.system("cls" if os.name == "nt" else "clear")
            continue
        try:
            argv = [t[1:-1] if len(t) > 1 and t[0] == t[-1] and t[0] in "\"'" else t
                    for t in shlex.split(line, posix=False)]
        except ValueError as e:
            print(paint("[x]  ", "31") + f"couldn't parse that: {e}")
            continue
        run(parser, argv)
        print()


def main():
    parser = build_parser()
    if getattr(sys, "frozen", False):  # remove the exe an update left behind
        import updater
        threading.Thread(target=updater.cleanup, daemon=True).start()
    if len(sys.argv) == 1 or sys.argv[1:] == ["menu"]:
        try:
            if len(sys.argv) == 1 and os.environ.get("VALSET_TERMINAL") != "1":
                try:
                    import tui
                    tui.run()
                    return 0
                except AppError as e:
                    warn(f"{e}  Using the simple menu instead.")
            menu(parser)
        except KeyboardInterrupt:
            pass
        except Exception:
            # double-clicked exe: keep the window open so the error can be read
            import traceback
            traceback.print_exc()
            input("\nSomething went wrong. Press Enter to close...")
            return 1
        return 0
    return run(parser, sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
