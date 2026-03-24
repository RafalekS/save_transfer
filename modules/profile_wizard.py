"""
Profile creation wizard — multi-page QDialog.
Page flow:
  1. Pick installed game  (scan WGS packages, cross-reference xgp_games.json)
  2. Game details         (name, handler type, xbox package prefix)
  3. Xbox WGS path        (auto-detect / browse, blob list)
  4. Steam path + files   (path template, resolve, file mapping table)
  5. Review + save
"""

import json
import logging
import os
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from modules.game_profile import (
    GameProfile,
    IdentifyRule,
    SteamFile,
    save_profile,
)
from modules.xbox_save import find_xbox_package, find_wgs_dir
from modules.steam_save import expand_steam_path

log = logging.getLogger(__name__)

XGP_GAMES_PATH = Path(__file__).parent.parent / "config" / "xgp_games.json"
PACKAGES_ROOT = Path(os.path.expandvars("%LOCALAPPDATA%")) / "Packages"

# Handler type descriptions shown to the user
HANDLER_DESCRIPTIONS = {
    "auto-detect": (
        "Auto-detect / Unknown.\n"
        "The app will inspect the WGS container structure and show all blobs for you to label.\n"
        "Use this when the game is not in the known list or you are unsure."
    ),
    "1c1f": (
        "Single container, single file.\n"
        "The game stores one save slot as a single data file inside the WGS container.\n"
        "Examples: Celeste, Oblivion Remastered, Manor Lords, Atomic Heart."
    ),
    "1cnf": (
        "Single container, multiple files.\n"
        "The game stores several save files (e.g. per kingdom, per character) inside one container.\n"
        "Examples: Hades, Sea of Stars, Chained Echoes, Super Fantasy Kingdom."
    ),
    "1cnf-folder": (
        "Folder-based saves.\n"
        "Each save slot is stored as a subfolder containing multiple files.\n"
        "Examples: Persona 5 Royal, Wo Long: Fallen Dynasty, Doom Eternal."
    ),
    "custom": (
        "Custom / unsupported handler.\n"
        "This game uses a non-standard save structure (e.g. encrypted, proprietary format).\n"
        "Examples: Starfield, Forza Horizon 5, Palworld, Control.\n"
        "You can still create a profile to document paths, but transfer may require manual steps."
    ),
}

HANDLER_ORDER = ["auto-detect", "1c1f", "1cnf", "1cnf-folder", "custom"]


def _load_xgp_games() -> list[dict]:
    if XGP_GAMES_PATH.exists():
        try:
            with open(XGP_GAMES_PATH, encoding="utf-8") as f:
                return json.load(f).get("games", [])
        except Exception:
            pass
    return []


def _scan_packages_with_saves() -> list[tuple[str, dict | None]]:
    """
    Scan %LOCALAPPDATA%\\Packages\\ for folders that have WGS save data.
    Returns list of (pkg_folder_name, xgp_game_dict_or_None), sorted: known games first,
    then unknown, both alphabetically within each group.
    """
    xgp_games = _load_xgp_games()
    pkg_lookup = {g["package"].lower(): g for g in xgp_games}

    results: list[tuple[str, dict | None]] = []
    if not PACKAGES_ROOT.exists():
        return results

    for pkg in PACKAGES_ROOT.iterdir():
        if not pkg.is_dir():
            continue
        wgs = pkg / "SystemAppData" / "wgs"
        if not wgs.exists():
            continue
        try:
            user_dirs = [d for d in wgs.iterdir() if d.is_dir()]
            has_saves = any(
                any(f.is_file() for f in ud.iterdir())
                for ud in user_dirs
            )
            if has_saves:
                game_data = pkg_lookup.get(pkg.name.lower())
                results.append((pkg.name, game_data))
        except OSError:
            continue

    # Sort: known games first (by game name), then unknown (by package name)
    known = sorted([(n, g) for n, g in results if g], key=lambda x: x[1]["name"].lower())
    unknown = sorted([(n, g) for n, g in results if not g], key=lambda x: x[0].lower())
    return known + unknown


class ProfileWizard(QDialog):
    """5-page wizard for creating or editing a game profile."""

    PAGE_PICK = 0
    PAGE_DETAILS = 1
    PAGE_XBOX = 2
    PAGE_STEAM = 3
    PAGE_REVIEW = 4

    def __init__(self, parent=None, edit_profile: GameProfile | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Game Profile" if edit_profile is None else "Edit Game Profile")
        self.setMinimumSize(660, 540)
        self.setModal(True)

        self.saved_profile_name: str = ""
        self._edit_profile = edit_profile
        self._xgp_games = _load_xgp_games()

        # Working data carried between pages
        self._xbox_package = ""        # full package folder name or prefix
        self._handler_args: dict = {}
        self._xbox_dir: Path | None = None
        self._steam_dir: Path | None = None

        self._build_ui()

        if edit_profile:
            self._pre_fill_from_profile(edit_profile)
            # Skip the pick-game page when editing
            self._stack.setCurrentIndex(self.PAGE_DETAILS)
            self._update_nav()
        else:
            # Auto-scan on open so the list is ready immediately
            self._do_scan()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        self._stack = QStackedWidget()
        root.addWidget(self._stack)

        self._stack.addWidget(self._page_pick())
        self._stack.addWidget(self._page_details())
        self._stack.addWidget(self._page_xbox())
        self._stack.addWidget(self._page_steam())
        self._stack.addWidget(self._page_review())

        nav = QHBoxLayout()
        self._btn_back = QPushButton("Back")
        self._btn_back.clicked.connect(self._go_back)
        self._btn_next = QPushButton("Next")
        self._btn_next.clicked.connect(self._go_next)
        self._btn_cancel = QPushButton("Cancel")
        self._btn_cancel.clicked.connect(self.reject)
        nav.addWidget(self._btn_back)
        nav.addStretch()
        nav.addWidget(self._btn_cancel)
        nav.addWidget(self._btn_next)
        root.addLayout(nav)

        self._update_nav()

    # ---- Page 1: Pick installed game ----

    def _page_pick(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(QLabel("<b>Step 1 of 5: Select an installed game</b>"))
        layout.addWidget(QLabel(
            "Games below are Xbox Game Pass titles found on this PC that have save data.\n"
            "Known games show their name and handler type. Unknown games show the package folder.\n"
            "Select one to pre-fill the wizard, or click Next to fill in details manually."
        ))

        scan_row = QHBoxLayout()
        self._btn_scan = QPushButton("Rescan")
        self._btn_scan.clicked.connect(self._do_scan)
        scan_row.addWidget(self._btn_scan)
        self._scan_status = QLabel("Scanning…")
        scan_row.addWidget(self._scan_status, stretch=1)
        layout.addLayout(scan_row)

        self._pick_search = QLineEdit()
        self._pick_search.setPlaceholderText("Filter list…")
        self._pick_search.textChanged.connect(self._filter_pick_list)
        layout.addWidget(self._pick_search)

        self._pick_list = QListWidget()
        self._pick_list.itemClicked.connect(self._on_pick_selected)
        self._pick_list.itemDoubleClicked.connect(self._go_next)
        layout.addWidget(self._pick_list)

        layout.addWidget(QLabel(
            "Not listed? Fill in the game name and details on the next page manually."
        ))
        return w

    def _do_scan(self) -> None:
        self._scan_status.setText("Scanning…")
        self._pick_list.clear()
        self._scan_results = _scan_packages_with_saves()
        self._render_pick_list(self._pick_search.text() if hasattr(self, "_pick_search") else "")
        self._scan_status.setText(f"{len(self._scan_results)} game(s) with saves found.")

    def _render_pick_list(self, query: str) -> None:
        self._pick_list.clear()
        q = query.lower()
        for pkg_name, game_data in self._scan_results:
            if game_data:
                display = f"{game_data['name']}   [{game_data['handler']}]"
                search_text = game_data["name"].lower()
            else:
                display = f"Unknown — {pkg_name}"
                search_text = pkg_name.lower()
            if q and q not in search_text:
                continue
            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, {"pkg_name": pkg_name, "game_data": game_data})
            if not game_data:
                from PyQt6.QtGui import QColor
                item.setForeground(QColor("#888"))
            self._pick_list.addItem(item)

    def _filter_pick_list(self, text: str) -> None:
        self._render_pick_list(text)

    def _on_pick_selected(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        pkg_name: str = data["pkg_name"]
        game_data: dict | None = data["game_data"]

        if game_data:
            self._details_name_edit.setText(game_data["name"])
            handler = game_data.get("handler", "auto-detect")
            # Map non-standard handlers (like-a-dragon, control, etc.) to "custom"
            if handler not in HANDLER_ORDER:
                handler = "custom"
            idx = self._details_handler_combo.findData(handler)
            if idx >= 0:
                self._details_handler_combo.setCurrentIndex(idx)
            self._xbox_package = game_data["package"]
            self._handler_args = game_data.get("handler_args", {})
        else:
            # Unknown game — use package folder name stripped of publisher suffix
            self._details_name_edit.setText("")
            self._details_handler_combo.setCurrentIndex(
                self._details_handler_combo.findData("auto-detect")
            )
            self._xbox_package = pkg_name
            self._handler_args = {}

        # Pre-fill the package edit on page 2
        self._details_pkg_edit.setText(self._xbox_package)
        self._update_nav()

    # ---- Page 2: Game details ----

    def _page_details(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(QLabel("<b>Step 2 of 5: Game details</b>"))

        layout.addWidget(QLabel("Game name:"))
        self._details_name_edit = QLineEdit()
        self._details_name_edit.setPlaceholderText("e.g. Super Fantasy Kingdom")
        self._details_name_edit.textChanged.connect(self._update_nav)
        layout.addWidget(self._details_name_edit)

        layout.addWidget(QLabel("Xbox package name / prefix:"))
        self._details_pkg_edit = QLineEdit()
        self._details_pkg_edit.setPlaceholderText(
            "e.g. HoodedHorse.SuperFantasyKingdom_znaey1dw2bdpr  (full name or prefix)"
        )
        layout.addWidget(self._details_pkg_edit)

        layout.addWidget(QLabel("Handler type:"))
        self._details_handler_combo = QComboBox()
        for h in HANDLER_ORDER:
            self._details_handler_combo.addItem(h, h)
        self._details_handler_combo.currentIndexChanged.connect(self._on_handler_changed)
        layout.addWidget(self._details_handler_combo)

        self._details_handler_desc = QLabel(HANDLER_DESCRIPTIONS["auto-detect"])
        self._details_handler_desc.setWordWrap(True)
        self._details_handler_desc.setStyleSheet(
            "color: #444; font-style: italic; "
            "background: #f5f5f5; border: 1px solid #ccc; padding: 6px; border-radius: 3px;"
        )
        layout.addWidget(self._details_handler_desc)

        layout.addStretch()
        return w

    def _on_handler_changed(self) -> None:
        h = self._details_handler_combo.currentData()
        self._details_handler_desc.setText(HANDLER_DESCRIPTIONS.get(h, ""))

    # ---- Page 3: Xbox WGS path ----

    def _page_xbox(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(QLabel("<b>Step 3 of 5: Xbox Game Pass save location</b>"))
        layout.addWidget(QLabel(
            "The WGS folder is under:\n"
            "  %LOCALAPPDATA%\\Packages\\<package>\\SystemAppData\\wgs\\\n"
            "Click Auto-detect or Browse to locate it."
        ))

        detect_row = QHBoxLayout()
        self._btn_xbox_detect = QPushButton("Auto-detect")
        self._btn_xbox_detect.clicked.connect(self._detect_xbox)
        detect_row.addWidget(self._btn_xbox_detect)
        self._btn_xbox_browse = QPushButton("Browse…")
        self._btn_xbox_browse.clicked.connect(self._browse_xbox)
        detect_row.addWidget(self._btn_xbox_browse)
        detect_row.addStretch()
        layout.addLayout(detect_row)

        layout.addWidget(QLabel("WGS path:"))
        self._xbox_wgs_label = QLabel("—")
        self._xbox_wgs_label.setWordWrap(True)
        layout.addWidget(self._xbox_wgs_label)

        layout.addWidget(QLabel("Save blobs found:"))
        self._xbox_blobs_text = QPlainTextEdit()
        self._xbox_blobs_text.setReadOnly(True)
        layout.addWidget(self._xbox_blobs_text)

        return w

    def _detect_xbox(self) -> None:
        pkg = self._details_pkg_edit.text().strip() or self._xbox_package
        if not pkg:
            QMessageBox.warning(self, "Missing", "Enter a package name on the previous page first.")
            return
        found = find_xbox_package(pkg)
        if not found:
            self._xbox_wgs_label.setText(f"Package not found matching: {pkg}")
            return
        wgs = find_wgs_dir(found)
        if not wgs:
            self._xbox_wgs_label.setText(f"Package found ({found.name}) but WGS folder missing.")
            return
        self._xbox_dir = wgs
        self._xbox_wgs_label.setText(str(wgs))
        self._show_xbox_blobs(wgs)

    def _browse_xbox(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select Xbox WGS Folder")
        if folder:
            self._xbox_dir = Path(folder)
            self._xbox_wgs_label.setText(folder)
            self._show_xbox_blobs(self._xbox_dir)

    def _show_xbox_blobs(self, wgs: Path) -> None:
        from modules.wgs_parser import get_blob_map
        blob_map = get_blob_map(wgs)
        if blob_map:
            lines = [f"{name}  ({p.stat().st_size} bytes)" for name, p in blob_map.items() if p.exists()]
            self._xbox_blobs_text.setPlainText("\n".join(lines))
        else:
            self._xbox_blobs_text.setPlainText("No blobs found.")

    # ---- Page 4: Steam path + file mapping ----

    def _page_steam(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(QLabel("<b>Step 4 of 5: Steam save location and file mapping</b>"))

        layout.addWidget(QLabel(
            "Path template — use %LOCALAPPDATA_LOW%, %LOCALAPPDATA%, %APPDATA%, or %STEAM_SAVES%:"
        ))
        self._steam_path_edit = QLineEdit()
        self._steam_path_edit.setPlaceholderText(
            r"e.g. %LOCALAPPDATA_LOW%\Super Fantasy Games\Super Fantasy Kingdom"
        )
        layout.addWidget(self._steam_path_edit)

        path_btns = QHBoxLayout()
        self._btn_steam_resolve = QPushButton("Resolve path")
        self._btn_steam_resolve.clicked.connect(self._detect_steam)
        path_btns.addWidget(self._btn_steam_resolve)
        self._btn_steam_browse = QPushButton("Browse…")
        self._btn_steam_browse.clicked.connect(self._browse_steam)
        path_btns.addWidget(self._btn_steam_browse)
        path_btns.addStretch()
        layout.addLayout(path_btns)

        self._steam_resolved_label = QLabel("—")
        self._steam_resolved_label.setWordWrap(True)
        layout.addWidget(self._steam_resolved_label)

        # File mapping table
        layout.addWidget(QLabel(
            "Map each Steam save file to a type key and label.\n"
            "Use wildcards (*) for dynamic filenames. Click 'Auto-fill' to populate from the folder."
        ))
        self._steam_map_table = QTableWidget()
        self._steam_map_table.setColumnCount(3)
        self._steam_map_table.setHorizontalHeaderLabels(["Steam Filename", "Type Key", "Label"])
        self._steam_map_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self._steam_map_table)

        tbl_btns = QHBoxLayout()
        self._btn_tbl_autofill = QPushButton("Auto-fill from folder")
        self._btn_tbl_autofill.clicked.connect(self._autofill_mapping)
        tbl_btns.addWidget(self._btn_tbl_autofill)
        self._btn_tbl_add = QPushButton("Add Row")
        self._btn_tbl_add.clicked.connect(lambda: self._add_mapping_row())
        tbl_btns.addWidget(self._btn_tbl_add)
        self._btn_tbl_remove = QPushButton("Remove Row")
        self._btn_tbl_remove.clicked.connect(self._remove_mapping_row)
        tbl_btns.addWidget(self._btn_tbl_remove)
        tbl_btns.addStretch()
        layout.addLayout(tbl_btns)

        return w

    def _detect_steam(self) -> None:
        template = self._steam_path_edit.text().strip()
        if not template:
            QMessageBox.warning(self, "Missing", "Enter a path template first.")
            return
        candidates = expand_steam_path(template)
        found = next((c for c in candidates if c.exists()), None)
        if not found:
            self._steam_resolved_label.setText(
                "Path not found: " + ", ".join(str(c) for c in candidates)
            )
            return
        self._steam_dir = found
        self._steam_resolved_label.setText(str(found))

    def _browse_steam(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select Steam Save Folder")
        if folder:
            self._steam_dir = Path(folder)
            self._steam_resolved_label.setText(folder)
            if not self._steam_path_edit.text().strip():
                self._steam_path_edit.setText(folder)

    def _add_mapping_row(self, name: str = "", type_key: str = "", label: str = "") -> None:
        row = self._steam_map_table.rowCount()
        self._steam_map_table.insertRow(row)
        self._steam_map_table.setItem(row, 0, QTableWidgetItem(name))
        self._steam_map_table.setItem(row, 1, QTableWidgetItem(type_key))
        self._steam_map_table.setItem(row, 2, QTableWidgetItem(label))

    def _remove_mapping_row(self) -> None:
        row = self._steam_map_table.currentRow()
        if row >= 0:
            self._steam_map_table.removeRow(row)

    def _autofill_mapping(self) -> None:
        if not self._steam_dir or not self._steam_dir.exists():
            QMessageBox.warning(self, "No folder", "Resolve or browse the Steam folder first.")
            return
        self._steam_map_table.setRowCount(0)
        for f in sorted(self._steam_dir.iterdir()):
            if f.is_file():
                stem = f.stem.lower().replace(" ", "_")
                self._add_mapping_row(f.name, stem, f.stem.replace("_", " ").title())

    # ---- Page 5: Review + save ----

    def _page_review(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(QLabel("<b>Step 5 of 5: Review and save</b>"))
        layout.addWidget(QLabel("Review the profile JSON below. Click 'Save Profile' to write it to disk."))
        self._review_text = QPlainTextEdit()
        self._review_text.setReadOnly(True)
        layout.addWidget(self._review_text)
        return w

    # ------------------------------------------------------------------
    # Build profile object
    # ------------------------------------------------------------------

    def _build_profile(self) -> GameProfile:
        steam_files = []
        for row in range(self._steam_map_table.rowCount()):
            n = self._steam_map_table.item(row, 0)
            t = self._steam_map_table.item(row, 1)
            lb = self._steam_map_table.item(row, 2)
            if n and n.text().strip():
                steam_files.append(SteamFile(
                    steam_name=n.text().strip(),
                    type=(t.text().strip() if t else ""),
                    label=(lb.text().strip() if lb else ""),
                ))

        handler = self._details_handler_combo.currentData()
        # Store "auto-detect" as "1cnf" for now (most common), wizard user can edit later
        if handler == "auto-detect":
            handler = "1cnf"

        return GameProfile(
            name=self._details_name_edit.text().strip(),
            xbox_package=self._details_pkg_edit.text().strip() or self._xbox_package,
            handler=handler,
            steam_path=self._steam_path_edit.text().strip() or (str(self._steam_dir) if self._steam_dir else ""),
            steam_files=steam_files,
            identify_rules=[],
            handler_args=self._handler_args,
        )

    def _build_review_json(self) -> str:
        p = self._build_profile()
        data = {
            "name": p.name,
            "xbox_package": p.xbox_package,
            "handler": p.handler,
            "steam_path": p.steam_path,
            "steam_files": [
                {"steam_name": sf.steam_name, "type": sf.type, "label": sf.label}
                for sf in p.steam_files
            ],
            "identify_rules": [],
        }
        return json.dumps(data, indent=2)

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _go_next(self, _=None) -> None:
        page = self._stack.currentIndex()

        if page == self.PAGE_REVIEW:
            self._save_profile()
            return

        if page == self.PAGE_DETAILS:
            if not self._details_name_edit.text().strip():
                QMessageBox.warning(self, "Required", "Enter a game name.")
                return
            # Auto-detect Xbox path when moving to page 3
            if not self._xbox_dir:
                self._detect_xbox()

        if page == self.PAGE_STEAM:
            self._review_text.setPlainText(self._build_review_json())

        self._stack.setCurrentIndex(page + 1)
        self._update_nav()

    def _go_back(self) -> None:
        page = self._stack.currentIndex()
        if page > 0:
            self._stack.setCurrentIndex(page - 1)
        self._update_nav()

    def _update_nav(self) -> None:
        page = self._stack.currentIndex()
        self._btn_back.setEnabled(page > 0)
        self._btn_next.setText("Save Profile" if page == self.PAGE_REVIEW else "Next")
        # Require a game name on page 2 before proceeding
        if page == self.PAGE_DETAILS:
            self._btn_next.setEnabled(bool(self._details_name_edit.text().strip()))
        else:
            self._btn_next.setEnabled(True)

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _save_profile(self) -> None:
        profile = self._build_profile()
        if not profile.name:
            QMessageBox.warning(self, "Missing", "Game name is required.")
            return
        try:
            path = save_profile(profile)
            self.saved_profile_name = profile.name
            QMessageBox.information(self, "Saved", f"Profile saved to:\n{path}")
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save profile:\n{e}")

    # ------------------------------------------------------------------
    # Pre-fill when editing an existing profile
    # ------------------------------------------------------------------

    def _pre_fill_from_profile(self, p: GameProfile) -> None:
        self._details_name_edit.setText(p.name)
        handler = p.handler if p.handler in HANDLER_ORDER else "custom"
        idx = self._details_handler_combo.findData(handler)
        if idx >= 0:
            self._details_handler_combo.setCurrentIndex(idx)
        self._xbox_package = p.xbox_package
        self._details_pkg_edit.setText(p.xbox_package)
        self._steam_path_edit.setText(p.steam_path)
        for sf in p.steam_files:
            self._add_mapping_row(sf.steam_name, sf.type, sf.label)
        self._handler_args = p.handler_args
        self._update_nav()
