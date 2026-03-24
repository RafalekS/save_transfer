"""
Game profile loader/saver.
Profiles live in config/games/<slug>.json.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

GAMES_DIR = Path(__file__).parent.parent / "config" / "games"


@dataclass
class SteamFile:
    steam_name: str   # target filename on Steam side (may contain * wildcard)
    type: str         # internal type key e.g. "permanent", "kingdom_human"
    label: str        # human-readable label


@dataclass
class IdentifyRule:
    type: str                       # type this rule resolves to
    required_keys: list[str] = field(default_factory=list)
    hint: str = ""                  # optional substring to disambiguate


@dataclass
class GameProfile:
    name: str
    xbox_package: str               # package name prefix for %LOCALAPPDATA%\Packages\ scan
    handler: str = "1cnf"           # 1c1f | 1cnf | 1cnf-folder | custom
    steam_path: str = ""            # path template; may use %LOCALAPPDATA_LOW%, %STEAM_SAVES%
    steam_files: list[SteamFile] = field(default_factory=list)
    identify_rules: list[IdentifyRule] = field(default_factory=list)
    handler_args: dict = field(default_factory=dict)
    slug: str = ""                  # derived from filename, not stored in JSON
    profile_path: Path | None = None

    def type_to_steam_name(self, type_key: str) -> str | None:
        for sf in self.steam_files:
            if sf.type == type_key:
                return sf.steam_name
        return None

    def all_types(self) -> list[str]:
        return [sf.type for sf in self.steam_files]


def _parse_profile(data: dict, path: Path) -> GameProfile:
    steam_files = [
        SteamFile(
            steam_name=sf["steam_name"],
            type=sf["type"],
            label=sf.get("label", sf["type"]),
        )
        for sf in data.get("steam_files", [])
    ]
    identify_rules = [
        IdentifyRule(
            type=r["type"],
            required_keys=r.get("required_keys", []),
            hint=r.get("hint", ""),
        )
        for r in data.get("identify_rules", [])
    ]
    slug = path.stem
    return GameProfile(
        name=data["name"],
        xbox_package=data.get("xbox_package", ""),
        handler=data.get("handler", "1cnf"),
        steam_path=data.get("steam_path", ""),
        steam_files=steam_files,
        identify_rules=identify_rules,
        handler_args=data.get("handler_args", {}),
        slug=slug,
        profile_path=path,
    )


def load_all_profiles() -> list[GameProfile]:
    if not GAMES_DIR.exists():
        return []
    profiles = []
    for p in sorted(GAMES_DIR.glob("*.json")):
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            profiles.append(_parse_profile(data, p))
        except Exception as e:
            log.warning("Failed to load profile %s: %s", p, e)
    return sorted(profiles, key=lambda p: p.name.lower())


def load_profile(path: Path) -> GameProfile:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return _parse_profile(data, path)


def save_profile(profile: GameProfile, path: Path | None = None) -> Path:
    if path is None:
        slug = re.sub(r"[^\w]+", "_", profile.name.lower()).strip("_")
        path = GAMES_DIR / f"{slug}.json"

    GAMES_DIR.mkdir(parents=True, exist_ok=True)

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
    if profile.handler_args:
        data["handler_args"] = profile.handler_args

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    profile.slug = path.stem
    profile.profile_path = path
    return path
