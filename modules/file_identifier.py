"""
Heuristic identification of Xbox WGS blob files.
Tries to classify a blob as a known save type using the game profile's identify_rules.
"""

import gzip
import json
import logging
from pathlib import Path
from dataclasses import dataclass

from modules.game_profile import IdentifyRule

log = logging.getLogger(__name__)

MIN_SIZE_BYTES = 64  # blobs smaller than this are likely metadata, skip them


@dataclass
class IdentifyResult:
    type: str
    label: str
    confidence: str  # "high" | "low"


def _read_content(path: Path) -> str | None:
    """Try to read the file as UTF-8 text. If it starts with gzip magic, decompress first."""
    try:
        raw = path.read_bytes()
        if raw[:2] == b"\x1f\x8b":  # gzip magic
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return None


def identify_blob(blob_path: Path, rules: list[IdentifyRule]) -> IdentifyResult | None:
    """
    Classify a blob file using the profile's identify_rules.
    Returns IdentifyResult or None if the file cannot be classified.
    """
    if not rules:
        return None

    size = blob_path.stat().st_size
    if size < MIN_SIZE_BYTES:
        log.debug("Skipping small file (%d bytes): %s", size, blob_path.name)
        return None

    content = _read_content(blob_path)
    if content is None:
        log.debug("Binary/unreadable blob: %s", blob_path.name)
        return None

    # Try JSON parse
    parsed = None
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        pass  # Not JSON — fall back to substring matching only

    matches: list[tuple[IdentifyResult, int]] = []  # (result, score)

    for rule in rules:
        score = 0

        # Check required_keys against parsed JSON
        if rule.required_keys:
            if parsed is None:
                continue  # JSON required but file isn't JSON
            if not all(k in parsed for k in rule.required_keys):
                continue
            score += len(rule.required_keys)

        # Check hint substring in raw content
        if rule.hint:
            if rule.hint.lower() in content.lower():
                score += 1
            elif rule.required_keys:
                # Has required_keys match but hint doesn't match — lower confidence
                score = max(score - 1, 0)
            else:
                continue  # hint-only rule and hint not found

        label = _type_to_label(rule.type)
        confidence = "high" if score >= 2 else "low"
        matches.append((IdentifyResult(type=rule.type, label=label, confidence=confidence), score))

    if not matches:
        return None

    # Return highest scoring match
    matches.sort(key=lambda x: x[1], reverse=True)
    return matches[0][0]


def _type_to_label(type_key: str) -> str:
    """Human-readable fallback label from type key."""
    return type_key.replace("_", " ").title()
