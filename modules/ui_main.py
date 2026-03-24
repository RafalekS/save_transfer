"""
MainWindow — Game Save Transfer Tool.
"""

import base64
import json
import logging
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QByteArray
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import modules.config_manager as config_manager
from modules.game_profile import GameProfile, load_all_profiles
from modules.xbox_save import SaveBlob, discover as xbox_discover
from modules.steam_save import SteamFileInfo, discover as steam_discover
from modules.transfer import (
    TransferError,
    backup_files,
    build_blob_type_map,
    transfer_xbox_to_steam,
    transfer_steam_to_xbox,
)

log = logging.getLogger(__name__)

DIRECTION_XTO_S = "xbox_to_steam"
DIRECTION_STO_X = "steam_to_xbox"

# Column indices — Xbox table
XB_COL_NAME = 0
XB_COL_SIZE = 1
XB_COL_MODIFIED = 2
XB_COL_TYPE = 3
XB_COL_CONF = 4

# Column indices — Steam table
ST_COL_NAME = 0
ST_COL_SIZE = 1
ST_COL_MODIFIED = 2
ST_COL_TYPE = 3
ST_COL_STATUS = 4


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n} {unit}"
        n //= 1024
    return f"{n} GB"


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M")


def _make_table(col_labels: list[str]) -> QTableWidget:
    t = QTableWidget()
    t.setColumnCount(len(col_labels))
    t.setHorizontalHeaderLabels(col_labels)
    t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    t.setAlternatingRowColors(True)
    t.verticalHeader().setVisible(False)

    h = t.horizontalHeader()
    h.setSectionsMovable(True)
    h.setSortIndicatorShown(True)
    h.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    # Do NOT call setStretchLastSection — prevents user resizing last column

    t.setSortingEnabled(False)  # enabled after population
    return t


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Game Save Transfer")
        self.setMinimumSize(900, 650)

        self._profiles: list[GameProfile] = []
        self._current_profile: GameProfile | None = None
        self._direction = DIRECTION_XTO_S
        self._blobs: list[SaveBlob] = []
        self._steam_files: list[SteamFileInfo] = []
        self._xbox_dir: Path | None = None
        self._steam_dir: Path | None = None

        # Debounce timer for column state saving
        self._save_state_timer = QTimer()
        self._save_state_timer.setSingleShot(True)
        self._save_state_timer.timeout.connect(self._save_column_states)

        self._build_ui()
        self._load_profiles()
        self._restore_geometry()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ---- Top bar: game selector ----
        top = QHBoxLayout()
        top.addWidget(QLabel("Game:"))
        self._game_combo = QComboBox()
        self._game_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._game_combo.currentIndexChanged.connect(self._on_game_changed)
        top.addWidget(self._game_combo)
        self._btn_add_game = QPushButton("+ Add Game")
        self._btn_add_game.clicked.connect(self._on_add_game)
        top.addWidget(self._btn_add_game)
        self._btn_edit_profile = QPushButton("Edit Profile")
        self._btn_edit_profile.clicked.connect(self._on_edit_profile)
        top.addWidget(self._btn_edit_profile)
        root.addLayout(top)

        # ---- Hint bar ----
        self._hint_label = QLabel("Select a game from the dropdown to begin.")
        self._hint_label.setStyleSheet("color: #555; font-style: italic; padding: 2px 0;")
        root.addWidget(self._hint_label)

        # ---- Middle: two panels with direction buttons ----
        mid = QHBoxLayout()
        mid.setSpacing(6)

        self._xbox_group = self._build_xbox_panel()
        mid.addWidget(self._xbox_group, stretch=5)

        dir_box = QVBoxLayout()
        dir_box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dir_box.setSpacing(4)
        self._btn_xtos = QPushButton("Xbox → Steam")
        self._btn_xtos.clicked.connect(lambda: self._set_direction(DIRECTION_XTO_S))
        self._btn_stox = QPushButton("Steam → Xbox")
        self._btn_stox.clicked.connect(lambda: self._set_direction(DIRECTION_STO_X))
        dir_box.addWidget(self._btn_xtos)
        dir_box.addWidget(self._btn_stox)
        mid.addLayout(dir_box)

        self._steam_group = self._build_steam_panel()
        mid.addWidget(self._steam_group, stretch=5)

        root.addLayout(mid, stretch=1)

        # ---- Bottom bar: backup + transfer ----
        bottom = QHBoxLayout()
        self._chk_backup = QCheckBox("Backup before transfer")
        self._chk_backup.setChecked(True)
        bottom.addWidget(self._chk_backup)
        bottom.addStretch()
        self._btn_transfer = QPushButton("Transfer")
        self._btn_transfer.setEnabled(False)
        self._btn_transfer.clicked.connect(self._on_transfer)
        bottom.addWidget(self._btn_transfer)
        root.addLayout(bottom)

        # ---- Log output ----
        self._log_output = QPlainTextEdit()
        self._log_output.setReadOnly(True)
        mono = QFont("Consolas", 9)
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self._log_output.setFont(mono)
        self._log_output.setMaximumHeight(160)
        root.addWidget(self._log_output)

        self._update_direction_buttons()

    def _build_xbox_panel(self) -> QGroupBox:
        group = QGroupBox("Xbox Game Pass")
        layout = QVBoxLayout(group)
        layout.setSpacing(4)

        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Path:"))
        self._xbox_path_edit = QLineEdit()
        self._xbox_path_edit.setPlaceholderText("Auto-detected or browse…")
        self._xbox_path_edit.editingFinished.connect(self._on_xbox_path_edited)
        path_row.addWidget(self._xbox_path_edit)
        self._btn_xbox_browse = QPushButton("Browse")
        self._btn_xbox_browse.clicked.connect(self._on_browse_xbox)
        path_row.addWidget(self._btn_xbox_browse)
        layout.addLayout(path_row)

        self._xbox_table = _make_table(["Blob Name", "Size", "Modified", "Identified As", "Confidence"])
        self._xbox_table.itemDoubleClicked.connect(self._on_xbox_item_double_clicked)
        self._xbox_table.horizontalHeader().sectionResized.connect(self._schedule_save_states)
        self._xbox_table.horizontalHeader().sectionMoved.connect(self._schedule_save_states)
        self._xbox_table.horizontalHeader().sortIndicatorChanged.connect(self._schedule_save_states)
        layout.addWidget(self._xbox_table)

        return group

    def _build_steam_panel(self) -> QGroupBox:
        group = QGroupBox("Steam")
        layout = QVBoxLayout(group)
        layout.setSpacing(4)

        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Path:"))
        self._steam_path_edit = QLineEdit()
        self._steam_path_edit.setPlaceholderText("Auto-detected or browse…")
        self._steam_path_edit.editingFinished.connect(self._on_steam_path_edited)
        path_row.addWidget(self._steam_path_edit)
        self._btn_steam_browse = QPushButton("Browse")
        self._btn_steam_browse.clicked.connect(self._on_browse_steam)
        path_row.addWidget(self._btn_steam_browse)
        layout.addLayout(path_row)

        self._steam_table = _make_table(["Filename", "Size", "Modified", "Type", "Status"])
        self._steam_table.horizontalHeader().sectionResized.connect(self._schedule_save_states)
        self._steam_table.horizontalHeader().sectionMoved.connect(self._schedule_save_states)
        self._steam_table.horizontalHeader().sortIndicatorChanged.connect(self._schedule_save_states)
        layout.addWidget(self._steam_table)

        return group

    # ------------------------------------------------------------------
    # Profile management
    # ------------------------------------------------------------------

    def _load_profiles(self) -> None:
        import json as _json
        from modules.xbox_save import PACKAGES_ROOT
        from modules.game_profile import GameProfile as _GP

        # Scan installed package folder names (fast — just directory names)
        installed_pkgs: set[str] = set()
        if PACKAGES_ROOT.exists():
            try:
                installed_pkgs = {p.name.lower() for p in PACKAGES_ROOT.iterdir() if p.is_dir()}
            except OSError:
                pass

        def _is_installed(xbox_package: str) -> bool:
            pkg_lower = xbox_package.lower()
            return (
                pkg_lower in installed_pkgs
                or any(name.startswith(pkg_lower) for name in installed_pkgs)
            )

        # Start with configured profile files, but only if the game is installed
        file_profiles = {p.xbox_package.lower(): p for p in load_all_profiles() if _is_installed(p.xbox_package)}

        # Also include any installed game from xgp_games.json that has NO profile file yet
        xgp_path = Path(__file__).parent.parent / "config" / "xgp_games.json"
        db_profiles: list[GameProfile] = []
        if xgp_path.exists():
            try:
                xgp_games = _json.loads(xgp_path.read_text(encoding="utf-8")).get("games", [])
                for g in xgp_games:
                    pkg = g.get("package", "")
                    if not pkg or not _is_installed(pkg):
                        continue
                    if pkg.lower() in file_profiles:
                        continue  # already have a configured profile
                    # Create a minimal profile from the DB entry
                    db_profiles.append(_GP(
                        name=g["name"],
                        xbox_package=pkg,
                        handler=g.get("handler", "1cnf"),
                        handler_args=g.get("handler_args", {}),
                    ))
            except Exception as e:
                log.warning("Failed to load xgp_games.json for dropdown: %s", e)

        self._profiles = sorted(
            list(file_profiles.values()) + db_profiles,
            key=lambda p: p.name.lower(),
        )

        self._game_combo.blockSignals(True)
        self._game_combo.clear()
        self._game_combo.addItem("— Select a game —", None)
        for p in self._profiles:
            self._game_combo.addItem(p.name, p)

        last = config_manager.get("last_game", "")
        if last:
            for i in range(self._game_combo.count()):
                if self._game_combo.itemText(i) == last:
                    self._game_combo.setCurrentIndex(i)
                    break

        self._game_combo.blockSignals(False)

        if self._game_combo.currentIndex() > 0:
            self._on_game_changed(self._game_combo.currentIndex())

    def _on_game_changed(self, index: int) -> None:
        profile = self._game_combo.itemData(index)
        if not isinstance(profile, GameProfile):
            self._current_profile = None
            self._clear_tables()
            return

        self._current_profile = profile
        config_manager.set("last_game", profile.name)
        config_manager.save()
        self._log(f"Game selected: {profile.name}")
        self._auto_detect_paths()

    def _on_add_game(self) -> None:
        # Import here to avoid circular at module load time
        from modules.profile_wizard import ProfileWizard
        wizard = ProfileWizard(self)
        if wizard.exec():
            self._load_profiles()
            # Select the newly added profile
            new_name = wizard.saved_profile_name
            for i in range(self._game_combo.count()):
                if self._game_combo.itemText(i) == new_name:
                    self._game_combo.setCurrentIndex(i)
                    break

    def _on_edit_profile(self) -> None:
        if not self._current_profile:
            QMessageBox.information(self, "No Game", "Select a game first.")
            return
        from modules.profile_wizard import ProfileWizard
        wizard = ProfileWizard(self, edit_profile=self._current_profile)
        if wizard.exec():
            self._load_profiles()

    # ------------------------------------------------------------------
    # Path detection and browsing
    # ------------------------------------------------------------------

    def _auto_detect_paths(self) -> None:
        if not self._current_profile:
            return

        # Always clear previous game's data first
        self._xbox_dir = None
        self._steam_dir = None
        self._xbox_path_edit.clear()
        self._steam_path_edit.clear()
        self._clear_tables()

        self._log("Auto-detecting paths…")

        # Xbox
        from modules.xbox_save import find_xbox_package, find_wgs_dir
        pkg = find_xbox_package(self._current_profile.xbox_package)
        if pkg:
            wgs = find_wgs_dir(pkg)
            if wgs:
                self._xbox_dir = wgs
                self._xbox_path_edit.setText(str(wgs))
                self._log(f"Xbox WGS path: {wgs}")
            else:
                self._log("Xbox package found but WGS folder missing.")
        else:
            self._log("Xbox Game Pass installation not found for this game.")

        # Steam
        from modules.steam_save import find_steam_save_dir
        steam_dir = find_steam_save_dir(self._current_profile)
        if steam_dir:
            self._steam_dir = steam_dir
            self._steam_path_edit.setText(str(steam_dir))
            self._log(f"Steam save path: {steam_dir}")
        else:
            self._log("Steam save folder not found for this game.")

        self._refresh_file_lists()

    def _on_browse_xbox(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select Xbox WGS Save Folder",
                                                   str(self._xbox_dir or ""))
        if folder:
            self._xbox_dir = Path(folder)
            self._xbox_path_edit.setText(folder)
            self._refresh_xbox_table()

    def _on_browse_steam(self) -> None:
        import os
        default = str(self._steam_dir) if self._steam_dir else str(
            Path(os.path.expandvars("%USERPROFILE%")) / "AppData" / "LocalLow"
        )
        folder = QFileDialog.getExistingDirectory(self, "Select Steam Save Folder", default)
        if folder:
            self._steam_dir = Path(folder)
            self._steam_path_edit.setText(folder)
            self._refresh_steam_table()

    def _on_xbox_path_edited(self) -> None:
        p = Path(self._xbox_path_edit.text().strip())
        if p.exists():
            self._xbox_dir = p
            self._refresh_xbox_table()

    def _on_steam_path_edited(self) -> None:
        p = Path(self._steam_path_edit.text().strip())
        if p.exists():
            self._steam_dir = p
            self._refresh_steam_table()

    # ------------------------------------------------------------------
    # Table population
    # ------------------------------------------------------------------

    def _refresh_file_lists(self) -> None:
        self._refresh_xbox_table()
        self._refresh_steam_table()
        self._update_transfer_button()

    def _refresh_xbox_table(self) -> None:
        if not self._xbox_dir or not self._current_profile:
            return

        from modules.xbox_save import list_save_blobs
        self._blobs = list_save_blobs(self._xbox_dir, self._current_profile)
        self._log(f"Found {len(self._blobs)} Xbox blob(s)")

        table = self._xbox_table
        table.setSortingEnabled(False)
        table.setRowCount(0)

        for row, blob in enumerate(self._blobs):
            table.insertRow(row)
            items = [
                QTableWidgetItem(blob.blob_name),
                QTableWidgetItem(_fmt_size(blob.size)),
                QTableWidgetItem(_fmt_time(blob.mtime)),
                QTableWidgetItem(blob.label or "Unknown — double-click to label"),
                QTableWidgetItem(blob.confidence or ""),
            ]
            if blob.type is None:
                for item in items:
                    item.setForeground(QColor("#cc0000"))
            items[XB_COL_SIZE].setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            for col, item in enumerate(items):
                item.setData(Qt.ItemDataRole.UserRole, row)  # preserve row reference
                table.setItem(row, col, item)

        table.setSortingEnabled(True)
        self._restore_column_state(table, "xbox_table")
        self._update_transfer_button()

    def _refresh_steam_table(self) -> None:
        if not self._steam_dir or not self._current_profile:
            return

        from modules.steam_save import list_steam_files
        self._steam_files = list_steam_files(self._steam_dir, self._current_profile)
        self._log(f"Found {len(self._steam_files)} Steam file(s)")

        table = self._steam_table
        table.setSortingEnabled(False)
        table.setRowCount(0)

        # Build a set of blob types available on Xbox side
        blob_types = {b.type for b in self._blobs if b.type}

        for row, sf in enumerate(self._steam_files):
            table.insertRow(row)

            if self._direction == DIRECTION_XTO_S:
                # Showing what will happen when copying Xbox → Steam
                status = "Will overwrite" if sf.path.exists() else "New file"
                status_color = QColor("#cc6600") if sf.path.exists() else QColor("#006600")
            else:
                # Steam → Xbox: indicate whether a matching Xbox blob exists to overwrite
                has_xbox_match = sf.type in blob_types
                status = "Ready" if has_xbox_match else "No Xbox blob — launch game on Xbox first"
                status_color = QColor("#006600") if has_xbox_match else QColor("#cc0000")

            items = [
                QTableWidgetItem(sf.steam_name),
                QTableWidgetItem(_fmt_size(sf.size)),
                QTableWidgetItem(_fmt_time(sf.mtime)),
                QTableWidgetItem(sf.label),
                QTableWidgetItem(status),
            ]
            items[ST_COL_SIZE].setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            items[ST_COL_STATUS].setForeground(status_color)
            for col, item in enumerate(items):
                table.setItem(row, col, item)

        table.setSortingEnabled(True)
        self._restore_column_state(table, "steam_table")
        self._update_transfer_button()

    def _clear_tables(self) -> None:
        self._xbox_table.setRowCount(0)
        self._steam_table.setRowCount(0)
        self._blobs = []
        self._steam_files = []
        self._update_transfer_button()

    # ------------------------------------------------------------------
    # Inline blob labelling (double-click Type column)
    # ------------------------------------------------------------------

    def _on_xbox_item_double_clicked(self, item: QTableWidgetItem) -> None:
        if item.column() != XB_COL_TYPE:
            return
        if not self._current_profile:
            return

        row = item.row()
        # Map visual row back to blob index (sort may have reordered)
        # We stored original row in UserRole of column 0
        orig_row_item = self._xbox_table.item(row, XB_COL_NAME)
        if orig_row_item is None:
            return
        orig_row = orig_row_item.data(Qt.ItemDataRole.UserRole)
        if orig_row is None or orig_row >= len(self._blobs):
            return
        blob = self._blobs[orig_row]

        combo = QComboBox()
        for sf in self._current_profile.steam_files:
            combo.addItem(sf.label, sf.type)
        if blob.type:
            idx = next(
                (i for i in range(combo.count()) if combo.itemData(i) == blob.type), 0
            )
            combo.setCurrentIndex(idx)

        self._xbox_table.setCellWidget(row, XB_COL_TYPE, combo)
        combo.currentIndexChanged.connect(lambda: self._apply_blob_label(combo, orig_row, row))

    def _apply_blob_label(self, combo: QComboBox, orig_row: int, visual_row: int) -> None:
        if orig_row >= len(self._blobs):
            return
        blob = self._blobs[orig_row]
        blob.type = combo.currentData()
        blob.label = combo.currentText()
        blob.confidence = "manual"

        # Replace combo with plain item
        item = QTableWidgetItem(blob.label)
        self._xbox_table.removeCellWidget(visual_row, XB_COL_TYPE)
        self._xbox_table.setItem(visual_row, XB_COL_TYPE, item)
        conf_item = QTableWidgetItem("manual")
        self._xbox_table.setItem(visual_row, XB_COL_CONF, conf_item)

        # Clear red colouring if now identified
        for col in range(self._xbox_table.columnCount()):
            it = self._xbox_table.item(visual_row, col)
            if it:
                it.setForeground(QColor())  # reset to default

        self._update_transfer_button()

    # ------------------------------------------------------------------
    # Direction toggle
    # ------------------------------------------------------------------

    def _set_direction(self, direction: str) -> None:
        self._direction = direction
        self._update_direction_buttons()
        self._refresh_steam_table()
        self._update_transfer_button()

    def _update_direction_buttons(self) -> None:
        active_style = "font-weight: bold;"
        inactive_style = ""
        if self._direction == DIRECTION_XTO_S:
            self._btn_xtos.setStyleSheet(active_style)
            self._btn_stox.setStyleSheet(inactive_style)
        else:
            self._btn_xtos.setStyleSheet(inactive_style)
            self._btn_stox.setStyleSheet(active_style)

    # ------------------------------------------------------------------
    # Transfer button state
    # ------------------------------------------------------------------

    def _update_transfer_button(self) -> None:
        enabled = False
        if self._current_profile and self._xbox_dir and self._steam_dir:
            if self._direction == DIRECTION_XTO_S:
                has_blobs = bool(self._blobs)
                all_identified = all(b.type is not None for b in self._blobs)
                enabled = has_blobs and all_identified
            else:
                enabled = bool(self._steam_files)
        self._btn_transfer.setEnabled(enabled)
        self._update_hint()

    # ------------------------------------------------------------------
    # Transfer execution
    # ------------------------------------------------------------------

    def _on_transfer(self) -> None:
        if not self._current_profile or not self._xbox_dir or not self._steam_dir:
            return

        backup_root = Path(__file__).parent.parent / config_manager.get("backup_dir", "backup")
        do_backup = self._chk_backup.isChecked()

        direction_label = "Xbox → Steam" if self._direction == DIRECTION_XTO_S else "Steam → Xbox"
        confirm = QMessageBox.question(
            self,
            "Confirm Transfer",
            f"Transfer saves: {direction_label}\n\nGame: {self._current_profile.name}\n\n"
            f"{'Backup will be created before overwriting.' if do_backup else 'WARNING: No backup will be made!'}\n\nProceed?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        self._log(f"--- Starting transfer: {direction_label} ---")
        try:
            if self._direction == DIRECTION_XTO_S:
                msgs = transfer_xbox_to_steam(
                    blobs=self._blobs,
                    profile=self._current_profile,
                    steam_dir=self._steam_dir,
                    backup_root=backup_root,
                    dry_run=False,
                )
            else:
                blob_type_map = build_blob_type_map(self._blobs)
                msgs = transfer_steam_to_xbox(
                    steam_files=self._steam_files,
                    blob_map=blob_type_map,
                    xbox_dir=self._xbox_dir,
                    backup_root=backup_root,
                    profile=self._current_profile,
                    dry_run=False,
                )

            for msg in msgs:
                self._log(msg)
            self._log("--- Transfer complete ---")
            QMessageBox.information(self, "Done", f"Transfer complete.\n\n" + "\n".join(msgs))
            self._refresh_file_lists()

        except TransferError as e:
            self._log(f"ERROR: {e}")
            QMessageBox.critical(self, "Transfer Failed", str(e))
        except Exception as e:
            self._log(f"UNEXPECTED ERROR: {e}")
            log.exception("Unexpected transfer error")
            QMessageBox.critical(self, "Transfer Failed", f"Unexpected error:\n{e}")

    # ------------------------------------------------------------------
    # Hint bar
    # ------------------------------------------------------------------

    def _update_hint(self) -> None:
        if not self._current_profile:
            self._hint_label.setText("Select a game from the dropdown to begin.")
            return
        if not self._xbox_dir and not self._steam_dir:
            self._hint_label.setText(
                "Paths not found automatically. Use Browse to locate save folders manually."
            )
            return
        if not self._xbox_dir:
            self._hint_label.setText(
                "Xbox WGS path not found. Browse to the wgs\\ folder under the game's package."
            )
            return
        if not self._steam_dir:
            self._hint_label.setText(
                "Steam save folder not found. Browse to the save directory or check the game profile."
            )
            return
        unknown_blobs = [b for b in self._blobs if b.type is None]
        if unknown_blobs:
            self._hint_label.setText(
                f"{len(unknown_blobs)} Xbox blob(s) not identified. "
                "Double-click the 'Identified As' cell to label them before transferring."
            )
            return
        direction_label = "Xbox → Steam" if self._direction == DIRECTION_XTO_S else "Steam → Xbox"
        self._hint_label.setText(
            f"Ready. Direction: {direction_label}. Click Transfer to proceed."
        )

    # ------------------------------------------------------------------
    # Log
    # ------------------------------------------------------------------

    def _log(self, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_output.appendPlainText(f"[{ts}] {message}")
        log.info(message)

    # ------------------------------------------------------------------
    # Column state persistence
    # ------------------------------------------------------------------

    def _schedule_save_states(self) -> None:
        self._save_state_timer.start(500)

    def _save_column_states(self) -> None:
        states = config_manager.get("column_states", {})
        states["xbox_table"] = self._xbox_table.horizontalHeader().saveState().toBase64().data().decode()
        states["steam_table"] = self._steam_table.horizontalHeader().saveState().toBase64().data().decode()
        config_manager.set("column_states", states)
        config_manager.save()

    def _restore_column_state(self, table: QTableWidget, key: str) -> None:
        states = config_manager.get("column_states", {})
        state_b64 = states.get(key)
        if not state_b64:
            return
        try:
            data = QByteArray.fromBase64(state_b64.encode())
            table.horizontalHeader().blockSignals(True)
            table.horizontalHeader().restoreState(data)
            table.horizontalHeader().blockSignals(False)
        except Exception as e:
            log.warning("Failed to restore column state for %s: %s", key, e)

    # ------------------------------------------------------------------
    # Window geometry persistence
    # ------------------------------------------------------------------

    def _restore_geometry(self) -> None:
        geom = config_manager.get("window_geometry")
        if geom:
            try:
                data = QByteArray.fromBase64(geom.encode())
                self.restoreGeometry(data)
            except Exception:
                pass

    def _save_geometry(self) -> None:
        data = self.saveGeometry().toBase64().data().decode()
        config_manager.set("window_geometry", data)
        config_manager.save()

    def closeEvent(self, event) -> None:
        self._save_state_timer.stop()
        self._save_column_states()
        self._save_geometry()
        super().closeEvent(event)
