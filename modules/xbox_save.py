"""
Xbox Game Pass (WGS) save discovery.
Finds the package folder, locates the WGS directory, and lists save blobs.
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from modules.game_profile import GameProfile
from modules.wgs_parser import get_blob_map
from modules.file_identifier import identify_blob

log = logging.getLogger(__name__)

PACKAGES_ROOT = Path(os.path.expandvars("%LOCALAPPDATA%")) / "Packages"


@dataclass
class SaveBlob:
    path: Path
    blob_name: str      # name from container file, or GUID filename as fallback
    size: int
    mtime: float
    type: str | None        # identified type, or None if unknown
    label: str | None       # human-readable label
    confidence: str | None  # "high" | "low" | None


def find_xbox_package(package_prefix: str) -> Path | None:
    """
    Scan %LOCALAPPDATA%\\Packages\\ for a folder matching the given prefix.
    Returns the first match or None.
    """
    if not PACKAGES_ROOT.exists():
        log.warning("Packages directory not found: %s", PACKAGES_ROOT)
        return None

    prefix_lower = package_prefix.lower()
    for p in PACKAGES_ROOT.iterdir():
        if p.is_dir() and p.name.lower().startswith(prefix_lower):
            log.debug("Found Xbox package: %s", p.name)
            return p

    log.debug("No Xbox package found matching prefix: %s", package_prefix)
    return None


def find_wgs_dir(package_path: Path) -> Path | None:
    """Return the wgs directory under the package's SystemAppData folder."""
    wgs = package_path / "SystemAppData" / "wgs"
    if wgs.exists():
        return wgs
    log.debug("WGS directory not found at %s", wgs)
    return None


def list_save_blobs(wgs_path: Path, profile: GameProfile) -> list[SaveBlob]:
    """
    List and classify save blobs in the WGS directory.
    Returns SaveBlob list sorted by modification time descending.
    """
    blob_map = get_blob_map(wgs_path)
    if not blob_map:
        log.warning("No blobs found in WGS path: %s", wgs_path)
        return []

    results: list[SaveBlob] = []
    for blob_name, file_path in blob_map.items():
        try:
            stat = file_path.stat()
        except OSError:
            continue

        result = identify_blob(file_path, profile.identify_rules)
        results.append(
            SaveBlob(
                path=file_path,
                blob_name=blob_name,
                size=stat.st_size,
                mtime=stat.st_mtime,
                type=result.type if result else None,
                label=result.label if result else None,
                confidence=result.confidence if result else None,
            )
        )

    results.sort(key=lambda b: b.mtime, reverse=True)
    return results


def discover(profile: GameProfile) -> tuple[Path | None, list[SaveBlob]]:
    """
    High-level: find the Xbox package, locate the WGS dir, list blobs.
    Returns (wgs_path, blobs). wgs_path is None if not found.
    """
    pkg = find_xbox_package(profile.xbox_package)
    if not pkg:
        return None, []

    wgs = find_wgs_dir(pkg)
    if not wgs:
        return None, []

    blobs = list_save_blobs(wgs, profile)
    return wgs, blobs
