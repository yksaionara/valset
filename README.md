<img src="assets/logo.png" width="96" alt="valset logo">

# valset (beta)

A live VALORANT match tracker plus a settings saver, in one small app. You only need the Riot Client open.

- **Live match:** rank, RR, peak rank, win rate, K/D, HS%, recent form, parties (duo / trio / stacks) and skins for all ten players. It fills in by itself when you hit agent select.
- **Player hints:** win and loss streaks, possible smurfs, players off their main agent, and a rough team edge.
- **Players you've met:** valset remembers who was in your matches. Tag someone *good* or *avoid*, write a note, and it shows up (with a warning for *avoid*) the next time you're in a game together.
- **Your session:** tonight's wins, losses and RR, your rank progress, and a recap card after every match.
- **Dodge:** a button in agent select, plus a hotkey that works from inside the game.
- **Settings saves:** save and load your settings per Riot account, without opening, closing and reopening the game. Before a load it shows exactly what would change.
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

valset runs full-screen in its terminal window, made to sit on a second monitor: drag it there and leave it. The board also fits narrow (portrait) windows. It has its own dark theme; turn **Dark background** off in Options to use your terminal's colours.

There are four screens: **1 Live**, **2 Players**, **3 Saves** and **4 Options**. Everything works with the mouse: click a tab, click a row to pick it, click it again (or double-click) to open it, and scroll with the wheel. The bottom line always shows the keys you can use right now, and you can click those too.

| Key | Does |
| --- | --- |
| `1` `2` `3` `4` / `Tab` | Live · Players · Saves · Options |
| `↑` `↓` | Pick a player (Live) or a row (other screens) |
| `Enter` | Save, load, change or open the highlighted row |
| `N` | Write a note on the highlighted player |
| `T` | Tag the highlighted player: good → avoid → none |
| `D` | Dodge (agent select only, asks Y to confirm) |
| `Del` | Delete the highlighted save, or clear a player's note and tag |
| `U` | Undo the last load (Saves screen) |
| `/` | Search players (Players screen) |
| `R` | Refresh your session (Live, between matches) |
| `Esc` | Cancel a question, close the player details |
| `Q` | Quit |

Text size is the terminal's own: **Ctrl + mouse wheel**. Turn **Mouse** off in Options if you'd rather select text with the mouse.

### Between matches: your session

While you're in the menus, the Live screen shows your rank with a bar to the next one, a recap of your last match (result, score, RR, K/D/A, ACS, HS%, where you placed, and the best player on each team) and tonight's games. "Tonight" is your run of games with no break longer than 2½ hours. The top bar keeps tonight's score (`Tonight 3W 1L +23 RR`) on every screen.

When agent select starts, valset dings and flashes its taskbar button so you notice from the other monitor. Turn that off with **Match alert** in Options.

### Player details, notes and tags

Click a player (or move to them with `↑` `↓`) to open their details: their last few competitive games, every time you've played with or against them, and your note. Press **N** to write a note and **T** to tag them *good* or *avoid*. The next time they're in your match, the tag and note show on their row, and an *avoid* tag also pops up a warning, in agent select too, so you still have time to dodge.

**Players** (`2`) lists everyone you've met, tagged players first. Your notes stay on this PC in `profiles\players.json`, next to your saves. Players who hide their name show as their agent; valset never shows or stores the name they hid.

The **NOTES** column also shows:

- **`met 2×`**: you've played with or against them before.
- **`▲3W streak`** / **`▼3L streak`**: they won or lost all of their last 3 competitive games.
- **`smurf?`**: a low account level for their rank, or a low level with a very high K/D and HS%. It's a hint, not proof.
- **`main Jett`**: they usually play another agent.

**Team edge** (in game) is a rough guess at your team's chances from rank, K/D and win rate. Treat it as a vibe check, not a prediction.

valset is built to stay out of your game's way. It's text only (no window toolkit, no browser), so it uses **about 25 MB of memory and well under 1% of one CPU core, and no GPU**. In the menus it asks the Riot Client on your PC whether a match has started, so it doesn't use the internet at all until agent select. Once a match has loaded it only checks now and then for the match ending. Connections to Riot are kept open and reused, and finished matches are saved on disk, so players you've already seen load instantly. Run `valset menu` for the simple numbered menu.

### Parties

A **`Duo 1`**, **`Trio 2`** or **`4-stack`** badge means those players queued together. **Your own party is exact:** it comes straight from Riot's party service, whether or not your duo is on your friends list. Friends in other parties come live from the Riot Client. For everyone else, valset checks their last few competitive games: Riot's match details record who was in whose party, so two players who queued together last time and are on the same team now are almost certainly queuing together again. Being random teammates before doesn't count.

The only stack it can't see is one queuing together for the first time in a while. Those players show as solo.

### Dodging

In agent select, press **D** and then **Y**, or press the hotkey (default **Ctrl+Shift+D**) **twice within 3 seconds**. The first press beeps, so one slip can't dodge you. You can change the hotkey or turn it off in Options.

Dodging gets Riot's normal penalty: **you lose RR in competitive and get a queue cooldown**, the same as quitting agent select any other way.

### Updates

valset checks this repo's [releases](https://github.com/yksaionara/valset/releases) when it starts (at most every 6 hours, one small request to GitHub). When a newer one is out, the top bar shows **↑ vX out**. Go to **Options** (`4`), pick **Install update** and press **Enter**:

- **valset.exe** downloads the new `valset.exe`, checks it against the SHA-256 checksum GitHub lists for it, swaps it in and restarts in the same window. Your saves and options are untouched. If anything fails, the old exe stays as it was.
- **Python source** replaces the `.py` files and `valset.bat`. Close valset and open it again to use them.
- **A git clone** is never overwritten; it tells you to `git pull`.

Nothing installs until you say so. Turn the start-up check off with **Check for updates** in Options. From the command line: `valset update` (or `valset update --check` to only look).

If you have v0.1.0-beta, download the new exe by hand once. That version didn't have the updater yet.

### Settings

**Save:** go to **Saves** (`3`), press **Enter** on **+ Save my current settings**, type a name (or just press Enter).

**Load:** close VALORANT, highlight a save and press **Enter**. valset shows what would change (for example `Sens 0.35 → 0.42 · Crosshair 6 changes · Audio same`). Press **Enter** again to load everything, or **P** to pick the parts (it starts with just the parts that differ). Your current settings are backed up first, and **U** undoes the load.

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
