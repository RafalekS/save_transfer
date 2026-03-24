"""
Profile creation wizard — multi-page QDialog.
Lets the user create a new game profile with auto-detection and manual overrides.
"""

import json
import logging
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
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
from modules.xbox_save import find_xbox_package, find_wgs_dir, list_save_blobs
from modules.steam_save import expand_steam_path, list_steam_files, find_steam_install

log = logging.getLogger(__name__)

XGP_GAMES_PATH = Path(__file__).parent.parent / "config" / "xgp_games.json"


def _load_xgp_games() -> list[dict]:
    if XGP_GAMES_PATH.exists():
        try:
            with open(XGP_GAMES_PATH, encoding="utf-8") as f:
                return json.load(f).get("games", [])
        except Exception:
            pass
    return []


class ProfileWizard(QDialog):
    """5-page wizard for creating or editing a game profile."""

    def __init__(self, parent=None, edit_profile: GameProfile | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Game Profile" if edit_profile is None else "Edit Game Profile")
        self.setMinimumSize(620, 480)
        self.setModal(True)

        self.saved_profile_name: str = ""
        self._edit_profile = edit_profile
        self._xgp_games = _load_xgp_games()

        # Working data
        self._name = ""
        self._xbox_package = ""
        self._handler = "1cnf"
        self._handler_args: dict = {}
        self._xbox_dir: Path | None = None
        self._steam_dir: Path | None = None
        self._steam_path_template = ""
        self._blob_type_map: dict[str, str] = {}   # blob_name → type
        self._steam_files: list[SteamFile] = []
        self._identify_rules: list[IdentifyRule] = []

        self._build_ui()

        if edit_profile:
            self._pre_fill_from_profile(edit_profile)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        self._stack = QStackedWidget()
        root.addWidget(self._stack)

        self._stack.addWidget(self._page1_game_name())
        self._stack.addWidget(self._page2_xbox())
        self._stack.addWidget(self._page3_steam())
        self._stack.addWidget(self._page4_mapping())
        self._stack.addWidget(self._page5_review())

        # Nav buttons
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

    # ---- Page 1: Game name ----

    def _page1_game_name(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(QLabel("<b>Step 1 of 5: Select or name the game</b>"))
        layout.addWidget(QLabel(
            "Choose from the known Xbox Game Pass game list, or type a new game name."
        ))

        self._p1_search = QLineEdit()
        self._p1_search.setPlaceholderText("Type to search…")
        self._p1_search.textChanged.connect(self._filter_xgp_list)
        layout.addWidget(self._p1_search)

        self._p1_list = QListWidget()
        self._p1_list.itemClicked.connect(self._on_xgp_game_selected)
        layout.addWidget(self._p1_list)
        self._populate_xgp_list("")

        layout.addWidget(QLabel("Game name:"))
        self._p1_name_edit = QLineEdit()
        self._p1_name_edit.setPlaceholderText("e.g. Super Fantasy Kingdom")
        self._p1_name_edit.textChanged.connect(self._update_nav)
        layout.addWidget(self._p1_name_edit)

        layout.addWidget(QLabel("Handler type:"))
        self._p1_handler_combo = QComboBox()
        for h in ["1c1f", "1cnf", "1cnf-folder", "custom"]:
            self._p1_handler_combo.addItem(h, h)
        layout.addWidget(self._p1_handler_combo)

        layout.addStretch()
        return w

    def _populate_xgp_list(self, query: str) -> None:
        self._p1_list.clear()
        q = query.lower()
        for g in self._xgp_games:
            if not q or q in g["name"].lower():
                item = QListWidgetItem(g["name"])
                item.setData(Qt.ItemDataRole.UserRole, g)
                self._p1_list.addItem(item)

    def _filter_xgp_list(self, text: str) -> None:
        self._populate_xgp_list(text)

    def _on_xgp_game_selected(self, item: QListWidgetItem) -> None:
        g = item.data(Qt.ItemDataRole.UserRole)
        self._p1_name_edit.setText(g["name"])
        idx = self._p1_handler_combo.findData(g.get("handler", "1cnf"))
        if idx >= 0:
            self._p1_handler_combo.setCurrentIndex(idx)
        self._xbox_package = g["package"]
        self._handler_args = g.get("handler_args", {})
        self._update_nav()

    # ---- Page 2: Xbox ----

    def _page2_xbox(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(QLabel("<b>Step 2 of 5: Xbox Game Pass save location</b>"))

        layout.addWidget(QLabel("Xbox package name prefix:"))
        self._p2_pkg_edit = QLineEdit()
        self._p2_pkg_edit.setPlaceholderText("e.g. HoodedHorse.SuperFantasyKingdom")
        layout.addWidget(self._p2_pkg_edit)

        detect_row = QHBoxLayout()
        self._btn_p2_detect = QPushButton("Auto-detect")
        self._btn_p2_detect.clicked.connect(self._detect_xbox)
        detect_row.addWidget(self._btn_p2_detect)
        self._btn_p2_browse = QPushButton("Browse WGS folder")
        self._btn_p2_browse.clicked.connect(self._browse_xbox)
        detect_row.addWidget(self._btn_p2_browse)
        layout.addLayout(detect_row)

        layout.addWidget(QLabel("Detected WGS path:"))
        self._p2_wgs_label = QLabel("—")
        self._p2_wgs_label.setWordWrap(True)
        layout.addWidget(self._p2_wgs_label)

        layout.addWidget(QLabel("Blobs found:"))
        self._p2_blobs_list = QPlainTextEdit()
        self._p2_blobs_list.setReadOnly(True)
        self._p2_blobs_list.setMaximumHeight(120)
        layout.addWidget(self._p2_blobs_list)

        layout.addStretch()
        return w

    def _detect_xbox(self) -> None:
        pkg = self._p2_pkg_edit.text().strip() or self._xbox_package
        if not pkg:
            QMessageBox.warning(self, "Missing", "Enter an Xbox package prefix first.")
            return
        found = find_xbox_package(pkg)
        if not found:
            self._p2_wgs_label.setText("Package not found.")
            return
        wgs = find_wgs_dir(found)
        if not wgs:
            self._p2_wgs_label.setText(f"Package found ({found.name}) but WGS folder missing.")
            return
        self._xbox_dir = wgs
        self._xbox_package = pkg
        self._p2_wgs_label.setText(str(wgs))
        # List blobs
        blobs = []
        from modules.wgs_parser import get_blob_map
        blob_map = get_blob_map(wgs)
        for name, path in blob_map.items():
            size = path.stat().st_size if path.exists() else 0
            blobs.append(f"{name}  ({size} bytes)")
        self._p2_blobs_list.setPlainText("\n".join(blobs) if blobs else "No blobs found")

    def _browse_xbox(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select Xbox WGS Folder")
        if folder:
            self._xbox_dir = Path(folder)
            self._p2_wgs_label.setText(folder)

    # ---- Page 3: Steam ----

    def _page3_steam(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(QLabel("<b>Step 3 of 5: Steam save location</b>"))
        layout.addWidget(QLabel(
            "Enter the Steam save path template.\n"
            "Use %LOCALAPPDATA_LOW%, %LOCALAPPDATA%, %APPDATA%, or %STEAM_SAVES%."
        ))

        layout.addWidget(QLabel("Path template:"))
        self._p3_path_edit = QLineEdit()
        self._p3_path_edit.setPlaceholderText(
            r"e.g. %LOCALAPPDATA_LOW%\Super Fantasy Games\Super Fantasy Kingdom"
        )
        layout.addWidget(self._p3_path_edit)

        detect_row = QHBoxLayout()
        self._btn_p3_detect = QPushButton("Resolve path")
        self._btn_p3_detect.clicked.connect(self._detect_steam)
        detect_row.addWidget(self._btn_p3_detect)
        self._btn_p3_browse = QPushButton("Browse folder")
        self._btn_p3_browse.clicked.connect(self._browse_steam)
        detect_row.addWidget(self._btn_p3_browse)
        layout.addLayout(detect_row)

        layout.addWidget(QLabel("Resolved path:"))
        self._p3_resolved_label = QLabel("—")
        self._p3_resolved_label.setWordWrap(True)
        layout.addWidget(self._p3_resolved_label)

        layout.addWidget(QLabel("Files found:"))
        self._p3_files_list = QPlainTextEdit()
        self._p3_files_list.setReadOnly(True)
        self._p3_files_list.setMaximumHeight(120)
        layout.addWidget(self._p3_files_list)

        layout.addStretch()
        return w

    def _detect_steam(self) -> None:
        template = self._p3_path_edit.text().strip()
        if not template:
            QMessageBox.warning(self, "Missing", "Enter a path template first.")
            return
        self._steam_path_template = template
        candidates = expand_steam_path(template)
        found = next((c for c in candidates if c.exists()), None)
        if not found:
            self._p3_resolved_label.setText("Path does not exist: " + ", ".join(str(c) for c in candidates))
            return
        self._steam_dir = found
        self._p3_resolved_label.setText(str(found))
        files = [p.name for p in found.iterdir() if p.is_file()]
        self._p3_files_list.setPlainText("\n".join(files) if files else "No files found")

    def _browse_steam(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select Steam Save Folder")
        if folder:
            self._steam_dir = Path(folder)
            self._p3_resolved_label.setText(folder)
            files = [p.name for p in self._steam_dir.iterdir() if p.is_file()]
            self._p3_files_list.setPlainText("\n".join(files) if files else "No files found")

    # ---- Page 4: File mapping ----

    def _page4_mapping(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(QLabel("<b>Step 4 of 5: Map save files to types</b>"))
        layout.addWidget(QLabel(
            "List each Steam save file and assign it a type key and label.\n"
            "Use wildcards (*) in the name for dynamic filenames (e.g. *_Player.sav)."
        ))

        self._p4_table = QTableWidget()
        self._p4_table.setColumnCount(3)
        self._p4_table.setHorizontalHeaderLabels(["Steam Filename", "Type Key", "Label"])
        self._p4_table.horizontalHeader().setStretchLastSection(False)
        layout.addWidget(self._p4_table)

        row_btns = QHBoxLayout()
        self._btn_p4_add = QPushButton("Add Row")
        self._btn_p4_add.clicked.connect(self._p4_add_row)
        row_btns.addWidget(self._btn_p4_add)
        self._btn_p4_remove = QPushButton("Remove Row")
        self._btn_p4_remove.clicked.connect(self._p4_remove_row)
        row_btns.addWidget(self._btn_p4_remove)
        row_btns.addStretch()
        layout.addLayout(row_btns)

        layout.addWidget(QLabel(
            "Auto-fill from Steam folder (if detected):"
        ))
        self._btn_p4_autofill = QPushButton("Auto-fill rows from Steam folder")
        self._btn_p4_autofill.clicked.connect(self._p4_autofill)
        layout.addWidget(self._btn_p4_autofill)

        layout.addStretch()
        return w

    def _p4_add_row(self, name: str = "", type_key: str = "", label: str = "") -> None:
        row = self._p4_table.rowCount()
        self._p4_table.insertRow(row)
        self._p4_table.setItem(row, 0, QTableWidgetItem(name))
        self._p4_table.setItem(row, 1, QTableWidgetItem(type_key))
        self._p4_table.setItem(row, 2, QTableWidgetItem(label))

    def _p4_remove_row(self) -> None:
        row = self._p4_table.currentRow()
        if row >= 0:
            self._p4_table.removeRow(row)

    def _p4_autofill(self) -> None:
        if not self._steam_dir or not self._steam_dir.exists():
            QMessageBox.warning(self, "No Steam folder", "Detect or browse the Steam folder on page 3 first.")
            return
        self._p4_table.setRowCount(0)
        for f in sorted(self._steam_dir.iterdir()):
            if f.is_file():
                stem = f.stem.lower().replace(" ", "_")
                self._p4_add_row(f.name, stem, f.stem.replace("_", " ").title())

    # ---- Page 5: Review ----

    def _page5_review(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(QLabel("<b>Step 5 of 5: Review and save</b>"))
        self._p5_preview = QPlainTextEdit()
        self._p5_preview.setReadOnly(True)
        layout.addWidget(self._p5_preview)
        return w

    def _build_review_json(self) -> str:
        profile = self._build_profile()
        data = {
            "name": profile.name,
            "xbox_package": profile.xbox_package,
            "handler": profile.handler,
            "steam_path": profile.steam_path,
            "steam_files": [
                {"steam_name": sf.steam_name, "type": sf.type, "label": sf.label}
                for sf in profile.steam_files
            ],
            "identify_rules": [
                {"type": r.type, "required_keys": r.required_keys, "hint": r.hint}
                for r in profile.identify_rules
            ],
        }
        return json.dumps(data, indent=2)

    def _build_profile(self) -> GameProfile:
        steam_files = []
        for row in range(self._p4_table.rowCount()):
            name_item = self._p4_table.item(row, 0)
            type_item = self._p4_table.item(row, 1)
            label_item = self._p4_table.item(row, 2)
            if name_item and name_item.text().strip():
                steam_files.append(SteamFile(
                    steam_name=name_item.text().strip(),
                    type=(type_item.text().strip() if type_item else ""),
                    label=(label_item.text().strip() if label_item else ""),
                ))

        return GameProfile(
            name=self._p1_name_edit.text().strip(),
            xbox_package=self._p2_pkg_edit.text().strip() or self._xbox_package,
            handler=self._p1_handler_combo.currentData(),
            steam_path=self._p3_path_edit.text().strip() or (str(self._steam_dir) if self._steam_dir else ""),
            steam_files=steam_files,
            identify_rules=[],  # identification rules are auto-generated or left empty for now
            handler_args=self._handler_args,
        )

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _go_next(self) -> None:
        page = self._stack.currentIndex()
        if page == 4:
            # Save
            self._save_profile()
            return

        if page == 0:
            name = self._p1_name_edit.text().strip()
            if not name:
                QMessageBox.warning(self, "Required", "Enter a game name.")
                return
            self._name = name
            self._handler = self._p1_handler_combo.currentData()
            # Pre-fill package on page 2
            self._p2_pkg_edit.setText(self._xbox_package)

        if page == 3:
            # Build review
            self._p5_preview.setPlainText(self._build_review_json())

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
        if page == 4:
            self._btn_next.setText("Save Profile")
        else:
            self._btn_next.setText("Next")
        # Disable Next on page 0 if no name entered
        if page == 0:
            self._btn_next.setEnabled(bool(self._p1_name_edit.text().strip()))
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
    # Pre-fill when editing
    # ------------------------------------------------------------------

    def _pre_fill_from_profile(self, p: GameProfile) -> None:
        self._p1_name_edit.setText(p.name)
        idx = self._p1_handler_combo.findData(p.handler)
        if idx >= 0:
            self._p1_handler_combo.setCurrentIndex(idx)
        self._xbox_package = p.xbox_package
        self._p2_pkg_edit.setText(p.xbox_package)
        self._p3_path_edit.setText(p.steam_path)
        self._steam_path_template = p.steam_path
        for sf in p.steam_files:
            self._p4_add_row(sf.steam_name, sf.type, sf.label)
        self._handler_args = p.handler_args
        self._update_nav()
