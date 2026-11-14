<img src="assets/logo.png" width="96" alt="valset logo">

# valset (beta)

A live VALORANT match tracker plus a settings saver, in one small app. You only need the Riot Client open.

- **Live match:** rank, RR, peak rank, win rate, K/D, HS%, level, parties (duo / trio / stacks) and skins for all ten players. It fills in by itself when you hit agent select.
- **Dodge:** a button in agent select, plus a hotkey that works from inside the game.
- **Settings saves:** save and load your settings per Riot account, without opening, closing and reopening the game.
- **Updates itself** from this repo's releases. It asks before installing.

> **Beta.** It works, but it isn't perfect yet. valset backs up the account before every load, and **Undo the last load** puts it back.

## What it saves

| Part | Where it lives |
| --- | --- |
| Sensitivity, keybinds, crosshair, audio, general | Riot's cloud settings for the account |
| Video (resolution, window mode, graphics) | `GameUserSettings.ini` on this PC |

Some settings belong to the PC rather than the account, like your mic or headset device and your monitor. Those are never copied, so they stay as they are on the account you load onto.

## Use it

1. Download **`valset-setup.exe`** from the [latest release](https://github.com/yksaionara/valset/releases) and run it. It installs just for you (no admin prompt) and adds valset to the Start menu, plus a desktop shortcut if you want one.
2. Open the Riot Client and log in.
3. Open **valset** from the Start menu.

Rather not install anything? Download **`valset.exe`** instead and put it in its own folder; your saves go in a `profiles` folder next to it. Uninstall from **Settings › Apps**. It asks whether to keep your saves (they're kept by default).

Windows SmartScreen may warn you because the exe isn't signed. Click **More info**, then **Run anyway**. If you'd rather run from source, install [Python 3.10+](https://www.python.org/downloads/) and double-click `valset.bat`.

valset runs full-screen in its terminal window. Drag it onto your second monitor; the board also fits narrow (portrait) windows.

There are three screens: **1 Live match**, **2 Saves** and **3 Options** (or press `Tab` / `←` `→`). Saves and Options are lists: move with `↑` `↓` and press **Enter**. The bottom line always shows the keys you can use right now.

| Key | Does |
| --- | --- |
| `1` `2` `3` | Live match · Saves · Options |
| `↑` `↓` | Pick a row (Saves, Options) or scroll the board |
| `Enter` | Save, load or change the highlighted row |
| `D` | Dodge (agent select only, asks Y to confirm) |
| `Del` | Delete the highlighted save (asks Y to confirm) |
| `U` | Undo the last load (Saves screen) |
| `Esc` | Cancel a question |
| `Q` | Quit |

Text size is the terminal's own: **Ctrl + mouse wheel**.

valset is built to stay out of your game's way. It's text only (no window toolkit, no browser), so it uses **about 25 MB of memory and well under 1% of one CPU core, and no GPU**. In the menus it asks the Riot Client on your PC whether a match has started, so it doesn't use the internet at all until agent select. Once a match has loaded it only checks now and then for the match ending. Connections to Riot are kept open and reused, and finished matches are saved on disk, so players you've already seen load instantly. Run `valset menu` for the simple numbered menu.

### Parties

A **`Duo 1`**, **`Trio 2`** or **`4-stack`** badge means those players queued together. **Your own party is exact:** it comes straight from Riot's party service, whether or not your duo is on your friends list. Friends in other parties come live from the Riot Client. For everyone else, valset checks their last few competitive games: Riot's match details record who was in whose party, so two players who queued together last time and are on the same team now are almost certainly queuing together again. Being random teammates before doesn't count.

The only stack it can't see is one queuing together for the first time in a while. Those players show as solo.

### Dodging

In agent select, press **D** and then **Y**, or press the hotkey (default **Ctrl+Shift+D**) **twice within 3 seconds**. The first press beeps, so one slip can't dodge you. You can change the hotkey or turn it off in Options.

Dodging gets Riot's normal penalty: **you lose RR in competitive and get a queue cooldown**, the same as quitting agent select any other way.

### Updates

valset checks this repo's [releases](https://github.com/yksaionara/valset/releases) when it starts (at most every 6 hours, one small request to GitHub). When a newer one is out, the top bar shows **⬆ vX out**. Go to **Options** (`3`), pick **Install update** and press **Enter**:

- **valset.exe** downloads the new `valset.exe`, checks it against the SHA-256 checksum GitHub lists for it, swaps it in and restarts in the same window. Your saves and options are untouched. If anything fails, the old exe stays as it was.
- **Python source** replaces the `.py` files and `valset.bat`. Close valset and open it again to use them.
- **A git clone** is never overwritten; it tells you to `git pull`.

Nothing installs until you say so. Turn the start-up check off with **Check for updates** in Options. From the command line: `valset update` (or `valset update --check` to only look).

If you have v0.1.0-beta, download the new exe by hand once. That version didn't have the updater yet.

### Settings

**Save:** go to **Saves** (`2`), press **Enter** on **+ Save my current settings**, type a name (or just press Enter).

**Load:** close VALORANT, highlight a save and press **Enter**, then **Enter** again to load everything, or **P** to tick just the parts you want (sens, keybinds, crosshair, audio, general, video). Your current settings are backed up first, and **U** undoes the load.

**Copy settings from one account to another:** save on account 1, switch to account 2 in the Riot Client, and load it. Saves from other accounts are listed under **Other accounts**.

## Commands

Use `valset.exe` instead of `python valset.py` if you have the exe.

```
python valset.py live          # live tracker in the terminal
python valset.py menu          # the old terminal menu
python valset.py update        # install a newer release from GitHub (--check to only look)
python valset.py --version
python valset.py status
python valset.py save "main"
python valset.py list
python valset.py show 1
python valset.py diff 1
python valset.py restore 1 --only sens keybinds
python valset.py export 1 main.zip
python valset.py import main.zip
```

## Releasing (for the maintainer)

1. Set `VERSION` near the top of `valset.py`, e.g. `VERSION = "0.3.0-beta"`, and commit.
2. Tag that commit with the same version and push the tag:
   ```
   git tag v0.3.0-beta
   git push origin main v0.3.0-beta
   ```
3. The **release** GitHub Action (`.github/workflows/release.yml`) checks that the tag matches `VERSION`, runs `build.ps1`, and publishes the release with `valset-setup.exe`, `valset.exe` and a source zip. A tag with a dash (`-beta`) becomes a pre-release. Every copy of valset sees it on its next start.

Edit the release notes on GitHub afterwards if you like. The first few lines show up in valset's Options screen.

To build on your own PC, run `.\build.ps1`. It puts `valset.exe` and `valset-setup.exe` in `dist\`. PyInstaller goes into a private `.build-venv` folder. The setup needs [Inno Setup 6](https://jrsoftware.org/isdl.php); use `.\build.ps1 -NoSetup` for just the exe.

## Notes

- Your saves stay on your PC in the `profiles` folder next to the script. Nothing gets uploaded anywhere apart from your own Riot settings.
- valset never asks for your password. It uses the login the Riot Client already has.
- Names, icons and skin pictures come from the public [valorant-api.com](https://valorant-api.com). Players who hide their name in game show up as their agent here too.
- If the Riot Client has been running for days it can lose its sign-in while the game keeps working. valset then says so. Sign in to the Riot Client again (or restart it) to fix it.
- It's Windows only. The source needs nothing beyond Python's standard library.
- This is a fan project and isn't made or endorsed by Riot Games. Use it at your own risk.
