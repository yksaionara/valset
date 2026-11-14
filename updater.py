"""valset updater: checks the GitHub releases of the valset repo and installs newer ones.

- valset.exe: downloads the new valset.exe from the release, checks its SHA-256 against
  the one GitHub lists, swaps it in (a running exe can be renamed, not overwritten) and
  restarts in the same window. The old exe is deleted on the next start.
- Python source: downloads the release's source and replaces the .py/.bat/README files.
- A git checkout is never touched: use git pull.

Stdlib only. One small request to api.github.com, at most every few hours.
"""
from __future__ import annotations
import ctypes
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
import io
from pathlib import Path

from valset import APP_DIR, LOCALAPPDATA, USER_AGENT, VERSION, AppError, _ssl_context

REPO = "yksaionara/valset"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases?per_page=15"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
STATE_FILE = LOCALAPPDATA / "valset" / "update.json"
CHECK_EVERY = 6 * 3600  # automatic checks; GitHub allows 60 unauthenticated calls an hour
SOURCE_FILES = (".py", ".bat", ".md")

FROZEN = bool(getattr(sys, "frozen", False))
EXE = Path(sys.executable).resolve() if FROZEN else None
OLD_EXE = EXE.with_name(EXE.stem + ".old.exe") if FROZEN else None


def parse_version(text):
    """'v0.2.0-beta' -> (0, 2, 0, 0, 'beta'); a final release sorts after its betas."""
    m = re.match(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-.]?([0-9A-Za-z.]+))?$", (text or "").strip())
    if not m:
        return None
    a, b, c, pre = m.groups()
    nums = (int(a), int(b or 0), int(c or 0))
    # 'beta.10' after 'beta.9': compare the number part as a number
    parts = tuple(int(x) if x.isdigit() else x for x in re.split(r"[.]", pre)) if pre else ()
    return nums + ((0, parts) if pre else (1, ()))


def is_newer(tag, current=VERSION):
    a, b = parse_version(tag), parse_version(current)
    try:
        return bool(a and b and a > b)
    except TypeError:  # oddly named tags, e.g. 'beta' vs 'rc.1' with a number: compare as text
        return str(a) > str(b)


def install_kind():
    if FROZEN:
        return "exe"
    if (APP_DIR / ".git").exists():
        return "git"
    return "source"


# --------------------------------------------------------------------------- state

def _load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(st):
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(st), encoding="utf-8")
    except OSError:
        pass


# --------------------------------------------------------------------------- network

def _get(url, accept="application/vnd.github+json", timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=_ssl_context(False))
    except urllib.error.HTTPError as e:
        if e.code == 403 and e.headers.get("X-RateLimit-Remaining") == "0":
            raise AppError("GitHub says too many update checks from this network. Try again in an hour.") from None
        raise AppError(f"GitHub returned HTTP {e.code} for {url}") from None
    except (urllib.error.URLError, OSError) as e:
        raise AppError(f"Couldn't reach GitHub to check for updates ({getattr(e, 'reason', e)}).") from None


def _release_info(r):
    assets = {a["name"]: a for a in r.get("assets", [])}
    exe = assets.get("valset.exe")
    return {
        "tag": r["tag_name"],
        "name": r.get("name") or r["tag_name"],
        "notes": (r.get("body") or "").strip(),
        "page": r.get("html_url") or RELEASES_PAGE,
        "published": r.get("published_at"),
        "prerelease": bool(r.get("prerelease")),
        "exe": {"url": exe["browser_download_url"], "size": exe.get("size"),
                "sha256": (exe.get("digest") or "").removeprefix("sha256:") or None} if exe else None,
        "source": f"https://api.github.com/repos/{REPO}/zipball/{r['tag_name']}",
    }


def latest_release():
    """The highest-versioned published release, betas included (valset is in beta)."""
    with _get(RELEASES_API) as resp:
        releases = json.loads(resp.read())
    best = None
    for r in releases:
        if r.get("draft") or not parse_version(r.get("tag_name")):
            continue
        if best is None or is_newer(r["tag_name"], best["tag_name"]):
            best = r
    return _release_info(best) if best else None


def check(force=False):
    """Returns the newest release if it's newer than this copy, else None.
    Without force, reuses the last answer for a few hours so starting valset costs nothing."""
    st = _load_state()
    if not force and time.time() - st.get("checked", 0) < CHECK_EVERY:
        rel = st.get("latest")
    else:
        rel = latest_release()
        _save_state({**st, "checked": time.time(), "latest": rel})
    return rel if rel and is_newer(rel["tag"]) else None


def short_notes(rel, lines=6):
    out = []
    for line in rel.get("notes", "").splitlines():
        line = re.sub(r"[*_`#]+", "", line).strip().lstrip("-• ").strip()
        if line and not line.lower().startswith(("full changelog", "**full changelog")):
            out.append(line)
    return out[:lines]


# --------------------------------------------------------------------------- install

def _download(url, dest, size=None, progress=None):
    h = hashlib.sha256()
    got = 0
    with _get(url, accept="application/octet-stream", timeout=30) as resp, open(dest, "wb") as f:
        total = size or int(resp.headers.get("Content-Length") or 0)
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            got += len(chunk)
            if progress and total:
                progress(got / total)
    return got, h.hexdigest()


def install(rel, progress=None):
    """Download and put the release in place. Returns a message; for the exe, call restart() next."""
    kind = install_kind()
    if kind == "git":
        raise AppError(f"This folder is a git checkout, so valset won't overwrite it. Run: git pull  ({rel['tag']} is out)")
    if kind == "exe":
        return _install_exe(rel, progress)
    return _install_source(rel, progress)


def _install_exe(rel, progress):
    a = rel.get("exe")
    if not a:
        raise AppError(f"{rel['tag']} has no valset.exe attached. Download it from {rel['page']}")
    new = EXE.with_name(EXE.stem + ".new.exe")
    try:
        got, digest = _download(a["url"], new, a.get("size"), progress)
        if a.get("size") and got != a["size"]:
            raise AppError("The download was cut off. Try again.")
        if a.get("sha256") and digest != a["sha256"].lower():
            raise AppError("The download doesn't match GitHub's checksum, so it wasn't installed. Try again.")
        with open(new, "rb") as f:
            if f.read(2) != b"MZ":
                raise AppError("The download isn't a Windows program, so it wasn't installed.")
        # Windows lets a running exe be renamed but not replaced
        try:
            OLD_EXE.unlink(missing_ok=True)
        except OSError:
            pass
        os.replace(EXE, OLD_EXE)
        try:
            os.replace(new, EXE)
        except OSError:
            os.replace(OLD_EXE, EXE)  # put the working one back
            raise
    except PermissionError:
        raise AppError(f"Windows didn't let valset replace {EXE.name}. Move it to a folder you own "
                       f"(not Program Files), or download it from {rel['page']}") from None
    except OSError as e:
        raise AppError(f"Couldn't install the update: {e}") from None
    finally:
        try:
            new.unlink(missing_ok=True)
        except OSError:
            pass
    return f"Updated to {rel['tag']}."


def _install_source(rel, progress):
    buf = io.BytesIO()
    with _get(rel["source"], accept="application/vnd.github+json", timeout=30) as resp:
        while chunk := resp.read(256 * 1024):  # GitHub doesn't send a size for these, so no %
            buf.write(chunk)
    if progress:
        progress(1.0)
    try:
        z = zipfile.ZipFile(buf)
    except zipfile.BadZipFile:
        raise AppError("GitHub sent a broken download. Try again.") from None
    files = {}
    for info in z.infolist():
        parts = info.filename.split("/")
        # zipball layout: <repo>-<sha>/<file>; only take top-level app files
        if len(parts) == 2 and parts[1].endswith(SOURCE_FILES) and not info.is_dir():
            files[parts[1]] = z.read(info)
    if "valset.py" not in files:
        raise AppError("That release doesn't look like valset, so nothing was changed.")
    for name, data in files.items():
        tmp = APP_DIR / (name + ".new")
        tmp.write_bytes(data)
        os.replace(tmp, APP_DIR / name)
    return f"Updated to {rel['tag']}. Close valset and open it again to use it."


def cleanup():
    """Delete the exe left over from the last update (it was still running then)."""
    if FROZEN and OLD_EXE.exists():
        for _ in range(10):  # the old copy may take a moment to exit
            try:
                OLD_EXE.unlink()
                return
            except OSError:
                time.sleep(0.5)


def _console_shared():
    """True if something else (cmd, PowerShell, a terminal) also uses this console window."""
    try:
        arr = (ctypes.c_uint32 * 16)()
        n = ctypes.windll.kernel32.GetConsoleProcessList(arr, 16)
        ours = {os.getpid(), os.getppid()}  # PyInstaller's onefile launcher is the parent
        return any(arr[i] not in ours for i in range(min(n, 16)))
    except Exception:
        return True


def restart(args=()):
    """Start the freshly installed exe. Double-clicked: it reuses this window (so it stays on
    the same monitor). Started from a shell: it opens its own window so the two don't fight
    over the keyboard. The caller should exit right after."""
    if not FROZEN:
        return False
    env = {k: v for k, v in os.environ.items() if not k.startswith(("_MEI", "_PYI"))}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"  # start as a new app, not a child of this one
    flags = 0x00000010 if _console_shared() else 0  # CREATE_NEW_CONSOLE
    for extra in (0x01000000, 0):  # CREATE_BREAKAWAY_FROM_JOB if allowed, so closing us can't take it down
        try:
            subprocess.Popen([str(EXE), *args], env=env, creationflags=flags | extra, close_fds=True)
            return True
        except OSError:
            continue
    return False


# --------------------------------------------------------------------------- CLI

def cmd_update(a):
    from valset import info, ok, warn, confirm
    kind = install_kind()
    where = {"exe": "valset.exe", "git": "git checkout", "source": "Python source"}[kind]
    info(f"valset {VERSION} ({where})")
    rel = check(force=True)
    if not rel:
        ok("You have the newest version.")
        return 0
    ok(f"{rel['tag']} is out.")
    for line in short_notes(rel, 10):
        print(f"    {line}")
    if getattr(a, "check", False):
        print(f"\n  Get it with:  valset update      or from {rel['page']}")
        return 0
    if kind == "git":
        warn("This folder is a git checkout, so valset won't overwrite it. Run: git pull")
        return 0
    if not getattr(a, "yes", False) and not confirm(f"  Install {rel['tag']} now?"):
        return 0

    def bar(p):
        sys.stdout.write(f"\r  Downloading... {p * 100:3.0f}%")
        sys.stdout.flush()
    msg = install(rel, bar)
    print()
    ok(msg)
    if kind == "exe":
        info("Open valset again to use it.")
    return 0
