"""
Save file transfer engine.
Handles backup, Xbox→Steam, and Steam→Xbox transfers.
"""

import logging
import shutil
from datetime import datetime
from pathlib import Path

from modules.game_profile import GameProfile
from modules.xbox_save import SaveBlob
from modules.steam_save import SteamFileInfo

log = logging.getLogger(__name__)


class TransferError(Exception):
    pass


def backup_files(file_paths: list[Path], backup_root: Path) -> Path:
    """
    Copy all given files into a timestamped backup subdirectory.
    Returns the backup directory path.
    Raises TransferError if any copy fails.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = backup_root / timestamp
    backup_dir.mkdir(parents=True, exist_ok=True)

    for src in file_paths:
        if not src.exists():
            log.warning("Backup: source file not found, skipping: %s", src)
            continue
        dest = backup_dir / src.name
        try:
            shutil.copy2(src, dest)
            log.info("Backed up: %s → %s", src.name, dest)
        except Exception as e:
            raise TransferError(f"Failed to backup {src.name}: {e}") from e

    log.info("Backup complete: %s (%d files)", backup_dir, len(list(backup_dir.iterdir())))
    return backup_dir


def transfer_xbox_to_steam(
    blobs: list[SaveBlob],
    profile: GameProfile,
    steam_dir: Path,
    backup_root: Path,
    dry_run: bool = False,
) -> list[str]:
    """
    Transfer Xbox blobs to the Steam save directory.
    Each blob must have a known type that maps to a steam_name in the profile.
    Backs up existing Steam files before writing.
    Returns a list of log messages describing actions taken.
    """
    messages: list[str] = []

    # Validate all blobs have a known type
    unidentified = [b for b in blobs if b.type is None]
    if unidentified:
        names = ", ".join(b.blob_name for b in unidentified)
        raise TransferError(
            f"Cannot transfer: {len(unidentified)} blob(s) not identified: {names}\n"
            "Label them manually before transferring."
        )

    # Build type → blob mapping (use most recently modified if duplicates)
    type_to_blob: dict[str, SaveBlob] = {}
    for blob in blobs:
        if blob.type not in type_to_blob or blob.mtime > type_to_blob[blob.type].mtime:
            type_to_blob[blob.type] = blob

    # Validate all required types are present
    missing = [sf.type for sf in profile.steam_files if sf.type not in type_to_blob]
    if missing:
        raise TransferError(f"Missing blobs for types: {', '.join(missing)}")

    # Backup existing Steam files
    steam_dir.mkdir(parents=True, exist_ok=True)
    existing_steam = [p for p in steam_dir.iterdir() if p.is_file()]
    if existing_steam and not dry_run:
        backup_dir = backup_files(existing_steam, backup_root)
        messages.append(f"Backed up {len(existing_steam)} existing Steam file(s) to {backup_dir.name}")

    # Copy blobs → Steam files
    for sf in profile.steam_files:
        blob = type_to_blob.get(sf.type)
        if not blob:
            continue

        # Resolve target filename (handle wildcard like *_Player.sav)
        steam_name = sf.steam_name
        if "*" in steam_name:
            # Use the blob's own name as the steam filename (preserve it)
            steam_name = blob.blob_name

        dest = steam_dir / steam_name
        if dry_run:
            messages.append(f"[DRY RUN] Would copy: {blob.blob_name} → {dest.name}")
        else:
            try:
                shutil.copy2(blob.path, dest)
                messages.append(f"Copied: {blob.blob_name} → {dest.name} ({sf.label})")
                log.info("Xbox→Steam: %s → %s", blob.blob_name, dest.name)
            except Exception as e:
                raise TransferError(f"Failed to copy {blob.blob_name} → {dest.name}: {e}") from e

    return messages


def transfer_steam_to_xbox(
    steam_files: list[SteamFileInfo],
    blob_map: dict[str, Path],  # {type → blob_path} built from existing Xbox blobs
    xbox_dir: Path,
    backup_root: Path,
    profile: GameProfile,
    dry_run: bool = False,
) -> list[str]:
    """
    Transfer Steam files to the Xbox WGS directory.
    Uses existing blob paths (preserves GUID filenames).
    Backs up existing Xbox blobs before writing.
    Returns a list of log messages.
    """
    messages: list[str] = []

    # Build type → steam file mapping
    type_to_steam: dict[str, SteamFileInfo] = {}
    for sf in steam_files:
        if sf.type not in type_to_steam or sf.mtime > type_to_steam[sf.type].mtime:
            type_to_steam[sf.type] = sf

    # Backup existing Xbox blobs
    existing_xbox = list(blob_map.values())
    if existing_xbox and not dry_run:
        backup_dir = backup_files(existing_xbox, backup_root)
        messages.append(f"Backed up {len(existing_xbox)} existing Xbox blob(s) to {backup_dir.name}")

    # Copy Steam files → Xbox blobs
    for sf_info in steam_files:
        dest_path = blob_map.get(sf_info.type)
        if dest_path is None:
            # No matching blob found — warn but don't fail
            messages.append(
                f"WARNING: No Xbox blob found for type '{sf_info.type}' ({sf_info.label}). "
                "Start the game on Xbox first to create save files, then retry."
            )
            log.warning("No blob path for type '%s'", sf_info.type)
            continue

        if dry_run:
            messages.append(f"[DRY RUN] Would copy: {sf_info.steam_name} → {dest_path.name}")
        else:
            try:
                shutil.copy2(sf_info.path, dest_path)
                messages.append(f"Copied: {sf_info.steam_name} → {dest_path.name} ({sf_info.label})")
                log.info("Steam→Xbox: %s → %s", sf_info.steam_name, dest_path.name)
            except Exception as e:
                raise TransferError(
                    f"Failed to copy {sf_info.steam_name} → {dest_path.name}: {e}"
                ) from e

    return messages


def build_blob_type_map(blobs: list[SaveBlob]) -> dict[str, Path]:
    """Build a {type → blob_path} dict from identified Xbox blobs (for Steam→Xbox transfers)."""
    result: dict[str, Path] = {}
    for blob in blobs:
        if blob.type and (blob.type not in result or blob.mtime > result.get(blob.type + "_mtime", 0)):
            result[blob.type] = blob.path
    return result
