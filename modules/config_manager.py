import json
import os
from pathlib import Path

CONFIG_PATH = Path(__file__).parent.parent / "config" / "config.json"

_DEFAULTS = {
    "last_game": "",
    "backup_dir": "backup",
    "window_geometry": None,
    "column_states": {
        "xbox_table": None,
        "steam_table": None,
    },
    "log_level": "INFO",
}

_config: dict = {}


def load() -> dict:
    global _config
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        except Exception:
            loaded = {}
    else:
        loaded = {}

    # Merge defaults so new keys are always present
    _config = {**_DEFAULTS, **loaded}
    _config.setdefault("column_states", {})
    return _config


def save() -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(_config, f, indent=2)


def get(key: str, default=None):
    return _config.get(key, default)


def set(key: str, value) -> None:
    _config[key] = value


def expand_path(path: str) -> str:
    """Expand environment variables and ~ in a path string."""
    if not path:
        return path
    return os.path.expandvars(os.path.expanduser(path))
