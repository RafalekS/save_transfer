"""
Steam save file discovery.
Handles %LOCALAPPDATA_LOW% and %STEAM_SAVES% path tokens in game profiles.
"""

import fnmatch
import logging
import os
import winreg
from dataclasses import dataclass
from pathlib import Path

from modules.game_profile import GameProfile

log = logging.getLogger(__name__)

LOCALAPPDATA_LOW = Path(os.path.expandvars("%USERPROFILE%")) / "AppData" / "LocalLow"


@dataclass
class SteamFileInfo:
    path: Path
    steam_name: str     # filename as it is on disk
    type: str           # type key from profile
    label: str          # human-readable label
    size: int
    mtime: float


def find_steam_install() -> Path | None:
    """Read Steam install path from registry."""
    keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Valve\Steam"),
    ]
    for hive, subkey in keys:
        try:
            with winreg.OpenKey(hive, subkey) as k:
                path_str, _ = winreg.QueryValueEx(k, "InstallPath")
                p = Path(path_str)
                if p.exists():
                    log.debug("Steam install found: %s", p)
                    return p
        except (FileNotFoundError, OSError):
            continue

    # Fallback: common locations
    for candidate in [
        Path("C:/Program Files (x86)/Steam"),
        Path("C:/Program Files/Steam"),
    ]:
        if candidate.exists():
            return candidate

    log.warning("Steam installation not found")
    return None


def _find_steam_userdata_saves(steam_install: Path, sub_path: str) -> list[Path]:
    """
    Search all Steam user accounts' userdata folders for a matching sub_path.
    Returns list of matching directories.
    """
    userdata = steam_install / "userdata"
    if not userdata.exists():
        return []
    results = []
    for user_dir in userdata.iterdir():
        if user_dir.is_dir():
            candidate = user_dir / sub_path
            if candidate.exists():
                results.append(candidate)
    return results


def expand_steam_path(path_template: str, steam_install: Path | None = None) -> list[Path]:
    """
    Expand a path template from a game profile.
    Returns a list of candidate paths (usually one, multiple if %STEAM_SAVES% expands to several users).
    """
    if not path_template:
        return []

    if "%LOCALAPPDATA_LOW%" in path_template.upper():
        expanded = path_template.replace("%LOCALAPPDATA_LOW%", str(LOCALAPPDATA_LOW))
        expanded = expanded.replace("%localappdata_low%", str(LOCALAPPDATA_LOW))
        return [Path(os.path.expandvars(expanded))]

    if "%STEAM_SAVES%" in path_template.upper():
        if steam_install is None:
            steam_install = find_steam_install()
        if steam_install is None:
            return []
        # Everything after %STEAM_SAVES%\ is the sub-path inside userdata/<id>/
        after = path_template.split("%STEAM_SAVES%")[-1].lstrip("\\/")
        # after = "FSD\\Saved\\SaveGames" for DRG
        # Try as relative to steam root first
        direct = steam_install / "steamapps" / "common" / after
        if direct.exists():
            return [direct]
        # Try inside userdata
        return _find_steam_userdata_saves(steam_install, after)

    # Standard env var expansion
    expanded = os.path.expandvars(os.path.expanduser(path_template))
    return [Path(expanded)]


def find_steam_save_dir(profile: GameProfile) -> Path | None:
    """Resolve the Steam save directory for a profile. Returns first valid path."""
    candidates = expand_steam_path(profile.steam_path)
    for c in candidates:
        if c.exists():
            return c
    return None


def list_steam_files(save_dir: Path, profile: GameProfile) -> list[SteamFileInfo]:
    """
    List save files in the Steam save directory, matched against profile steam_files.
    Unmatched files are not included.
    """
    if not save_dir or not save_dir.exists():
        return []

    results: list[SteamFileInfo] = []
    for sf in profile.steam_files:
        # Support wildcard in steam_name (e.g. "*_Player.sav")
        pattern = sf.steam_name
        matched = [p for p in save_dir.iterdir() if p.is_file() and fnmatch.fnmatch(p.name, pattern)]
        if not matched:
            log.debug("No Steam file found matching '%s' in %s", pattern, save_dir)
            continue
        for p in matched:
            try:
                stat = p.stat()
            except OSError:
                continue
            results.append(
                SteamFileInfo(
                    path=p,
                    steam_name=p.name,
                    type=sf.type,
                    label=sf.label,
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                )
            )

    results.sort(key=lambda f: f.mtime, reverse=True)
    return results


def discover(profile: GameProfile) -> tuple[Path | None, list[SteamFileInfo]]:
    """High-level: find Steam save dir and list files."""
    save_dir = find_steam_save_dir(profile)
    if not save_dir:
        return None, []
    files = list_steam_files(save_dir, profile)
    return save_dir, files
