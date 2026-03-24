# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A PyQt6 GUI application to transfer game save files between Xbox Game Pass and Steam, in both directions. The user manually performed this for Super Fantasy Kingdom and wants a general-purpose tool supporting any game.

## Reference Material

These links were the basis for the project. Check them if clarification is needed on save formats or transfer mechanics:

- Steam guide: https://steamcommunity.com/sharedfiles/filedetails/?id=2286193294
- Reddit guide (Deep Rock Galactic, general WGS approach): https://www.reddit.com/r/DeepRockGalactic/comments/e7hptr/how_to_transfer_your_steam_save_to_windows_10_and/
- Open source save sync tool (reference implementation): https://github.com/cdleveille/deep-rock-galactic-save-sync

**Note:** Some links are Discord channels requiring login — if they can't be accessed, ask Rafal to provide the content.

## Real-World Example: Super Fantasy Kingdom

Rafal manually transferred this game. Use it as the reference case for testing.

**Xbox (Game Pass) save folder:**
```
C:\Users\r_sta\AppData\Local\Packages\HoodedHorse.SuperFantasyKingdom_znaey1dw2bdpr\SystemAppData\wgs\0009000002414F5B_000000000000000000000000713AFEB2\334F9B15E49B4E1ABAAD5262604A8DA6\
```

Files found in that folder (all unnamed GUIDs):
```
1E74A3FC915B49C3AB62E942F35693B3   34B   (metadata/container ref — skip)
3BA8A962198A43A9BFEB5575BFF2786E   6KB
47F7401EBA7643BBA8937E019E2AB6EB   6KB
6AB2B0059A1C4B639425547CC8403C99   27KB
7BFA1CE4CF504EA3A593DB9FC3A9E278   26KB
A3EDEFEF7AB14840B9CB0A0578961B37   27KB
B55D9D6548C5435ABBAC2E69ACA3E85F   26KB
container.18                        1KB   (container metadata)
```

The 6 data files are 3 saves + 3 backups. Pairs share near-identical sizes with different timestamps.

**Steam save folder:**
```
C:\Users\r_sta\AppData\LocalLow\Super Fantasy Games\Super Fantasy Kingdom\
```

Steam files:
```
1_human_kingdom.data
1_undead_kingdom.data
1_permanent.data
```
(Backups are `.bak` variants — delete them after transfer, they'll be stale.)

**Identifying which Xbox blob is which:**
- `permanent.data` (player profile): JSON starts with `{"version":1,"identifier":"...","stars":16,"totalKingdoms":2,"totalWins":1,"totalRuns":47,...}`
- `human_kingdom.data` / `undead_kingdom.data`: JSON contains `"hero"` field near the start — use the hero name to tell them apart
- The `1` prefix in Steam filenames = profile number

## Running the App

```bash
python main.py
```

Syntax check only:
```bash
python -m py_compile main.py && python -m py_compile modules/*.py
```

## Project Structure

```
save_transfer/
├── main.py                  # Entry point
├── config/
│   ├── config.json          # Persisted settings, window geometry, game profiles
│   ├── assets/              # App icons
│   └── games/               # Per-game profile definitions (JSON)
├── modules/
│   ├── ui_main.py           # Main window layout
│   ├── game_profile.py      # Game profile loading/matching logic
│   ├── xbox_save.py         # Xbox WGS save path discovery and file parsing
│   ├── steam_save.py        # Steam save path discovery and file handling
│   ├── transfer.py          # Core copy/rename/backup transfer logic
│   └── file_identifier.py   # Heuristic logic to identify unnamed save files
├── help/
│   ├── TODO.md
│   └── README.md
├── backup/
├── logs/
└── .gitattributes
```

## Key Domain Knowledge

### Xbox (Game Pass) Save Location

Saves live under:
```
C:\Users\<user>\AppData\Local\Packages\<PackageID>\SystemAppData\wgs\<UserID>\<ContainerID>\
```

Files are unnamed blobs (GUIDs). A `container.N` metadata file exists alongside them. Files come in pairs: the save data and a `.bak` backup. Content is plain JSON (readable in a text editor).

### Steam Save Location

Named files in a game-specific path under `AppData\Local`, `AppData\LocalLow`, `AppData\Roaming`, or the Steam `userdata` folder. File names are meaningful (e.g. `1_human_kingdom.data`, `1_permanent.data`).

### Identifying Unnamed Xbox Blobs

Read each file as text and apply heuristics:
- Profile/permanent save: contains `"identifier"`, `"stars"`, `"totalKingdoms"`, `"totalRuns"`
- Kingdom saves: contain `"hero"` field near the start; differentiate by hero name
- Skip files smaller than ~100 bytes (likely metadata/container refs)
- Skip `.bak` equivalents (duplicates of the main save with older timestamps)

### Transfer Flow

**Xbox → Steam:**
1. Discover Xbox WGS container folder for selected game
2. Read and classify all blob files using heuristics
3. Back up existing Steam saves
4. Copy blobs to Steam folder, renaming to Steam conventions
5. Remove or warn about stale `.bak` files in Steam folder

**Steam → Xbox:**
1. Locate Steam save files
2. Back up existing Xbox blobs
3. Copy Steam files into WGS container folder using existing blob filenames (preserve names)
4. Update `container.N` if needed

### Game Profile Format (`config/games/<game>.json`)

Each game profile defines:
- `name`: display name
- `xbox_package`: partial or full package name for matching under `%LOCALAPPDATA%\Packages\`
- `xbox_container_pattern`: optional regex to match the WGS container subfolder
- `steam_path`: path template using `%APPDATA%`, `%LOCALAPPDATA%`, `%LOCALAPPDATA_LOW%`
- `steam_files`: list of `{ "name": "1_human_kingdom.data", "type": "kingdom_human" }` mappings
- `identify_by`: field or heuristic key used to classify Xbox blobs

## Notes

- `%LOCALAPPDATA_LOW%` is not a real Windows env var — expand it as `%USERPROFILE%\AppData\LocalLow`
- Always back up before overwriting saves
- Never hardcode user paths; resolve at runtime via `os.path.expandvars()` / `os.path.expanduser()`
- Xbox WGS folders contain both save data and backup blobs — use file size and recency to prefer the primary copy
