# valset (beta)

Save and load your VALORANT settings per Riot account. You only need the Riot Client open, and you never have to open, close and reopen the game.

> **Beta.** It works, but it isn't perfect yet. valset backs up the account before every load, and **Undo the last load** puts it back.

## What it saves

| Part | Where it lives |
| --- | --- |
| Sensitivity, keybinds, crosshair, audio, general | Riot's cloud settings for the account |
| Video (resolution, window mode, graphics) | `GameUserSettings.ini` on this PC |

Some settings belong to the PC rather than the account, like your mic or headset device and your monitor. Those are never copied, so they stay as they are on the account you load onto.

## Use it

1. Download `valset.exe` from the [latest release](https://github.com/yksaionara/valset/releases) and put it in its own folder, because your saves go in a `profiles` folder next to it.
2. Open the Riot Client and log in.
3. Double-click `valset.exe`.

Windows SmartScreen may warn you because the exe isn't signed. Click **More info**, then **Run anyway**. If you'd rather run from source, install [Python 3.10+](https://www.python.org/downloads/) and double-click `valset.bat`.

```
  1  Save my settings
  2  Load a save onto this account
  3  Look at my saves
  4  Undo the last load
  5  Delete a save
  6  Advanced (type commands)
```

The top of the menu shows the account you're logged in as, and it updates by itself when you switch accounts.

**Copy settings from one account to another:** log in to account 1 and pick **Save**. Switch to account 2 and pick **Load**. Close VALORANT before you load, and your settings are there the next time you launch it.

## Commands

Use `valset.exe` instead of `python valset.py` if you have the exe.

```
python valset.py status
python valset.py save "main"
python valset.py list
python valset.py show 1
python valset.py diff 1
python valset.py restore 1 --only sens keybinds
python valset.py export 1 main.zip
python valset.py import main.zip
```

## Notes

- Your saves stay on your PC in the `profiles` folder next to the script. Nothing gets uploaded anywhere apart from your own Riot settings.
- valset never asks for your password. It uses the login the Riot Client already has.
- It's Windows only. The source needs nothing beyond Python's standard library.
- This is a fan project and isn't made or endorsed by Riot Games. Use it at your own risk.
