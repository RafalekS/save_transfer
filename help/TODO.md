# TODO — Game Save Transfer Tool

## Status
All phases complete — awaiting user testing.

## Completed

### ✅ Phase 1 — Scaffolding
- .gitattributes (LF normalisation)
- Folder structure (config/, modules/, help/, backup/, logs/)
- config/config.json defaults
- modules/config_manager.py
- main.py bootstrap

### ✅ Phase 2 — WGS Binary Parser
- modules/wgs_parser.py
  - parse_containers_index() — reads containers.index binary format
  - parse_container_file() — reads container.X blob name → GUID mapping
  - get_blob_map() — high-level dict {blob_name → file_path}
  - Fallback: raw GUID file scan if container parsing fails

### ✅ Phase 3 — Save Discovery
- modules/game_profile.py + GameProfile dataclass
- config/games/super_fantasy_kingdom.json
- config/games/deep_rock_galactic.json
- config/xgp_games.json (45+ games from Z1ni/XGP-save-extractor)
- modules/xbox_save.py — package detection, WGS discovery, blob listing
- modules/steam_save.py — Steam path expansion, file listing
- modules/file_identifier.py — JSON key heuristics for blob classification

### ✅ Phase 4 — Transfer Engine
- modules/transfer.py
  - backup_files() — timestamped backup
  - transfer_xbox_to_steam()
  - transfer_steam_to_xbox()
  - build_blob_type_map()

### ✅ Phase 5 — Main UI
- modules/ui_main.py — MainWindow
  - Game selector dropdown with last-used persistence
  - Direction toggle (Xbox→Steam / Steam→Xbox)
  - Xbox panel: path + blob table with inline type labelling (double-click)
  - Steam panel: path + file table
  - Backup checkbox + Transfer button (disabled until valid state)
  - Log output (QPlainTextEdit)
  - Column state persistence (debounced 500ms QTimer)
  - Window geometry persistence

### ✅ Phase 6 — Profile Wizard
- modules/profile_wizard.py — ProfileWizard(QDialog)
  - Page 1: game name / xgp_games.json searchable lookup (45+ games)
  - Page 2: Xbox package + auto-detect WGS
  - Page 3: Steam path template + resolve
  - Page 4: file mapping table (auto-fill from Steam folder)
  - Page 5: review JSON + save

### ✅ Phase 7 — Polish
- Window geometry persistence
- Column state persistence (debounced QTimer 500ms, save on close)
- Logging to logs/save_transfer.log
- All modules syntax-checked clean

## Pending User Testing
- [ ] GUI launches: `python main.py`
- [ ] Super Fantasy Kingdom Xbox path auto-detected
- [ ] Steam path auto-detected
- [ ] Xbox blob table populates + identifies blobs correctly
- [ ] Transfer Xbox→Steam works (with backup)
- [ ] Transfer Steam→Xbox works (with backup)
- [ ] Add Game wizard works for a new game
- [ ] Column resize/reorder persists between sessions
- [ ] Window position persists between sessions

## Known Issues / Notes
- %LOCALAPPDATA_LOW% is not a real Windows env var — expanded as %USERPROFILE%\AppData\LocalLow
- containers.index parsing may need tuning per game — fallback scan is in place
- Deep Rock Galactic: binary .sav file, no JSON identification — transferred as-is
- Steam→Xbox requires the game to have been launched on Xbox first (to create blob files)
