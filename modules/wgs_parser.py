"""
WGS (Xbox Game Pass) container binary format parser.

References:
  - Z1ni/XGP-save-extractor (Python)
  - Fr33dan/GPSaveConverter — XboxContainerIndex.cs, XboxFileContainer.cs
  - AlDrag/game-pass-save-file-decoder

Folder layout:
  %LOCALAPPDATA%\\Packages\\<Package>\\SystemAppData\\wgs\\<UserID>\\
  ├── containers.index          <- binary index
  └── <ContainerFolder>\\
      ├── container.X           <- blob name → GUID file mapping
      └── <GUID-named files>    <- actual save data
"""

import logging
import struct
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

CONTAINER_FILE_HEADER = b"\x04\x00\x00\x00\x14"
ENTRY_BYTE_LENGTH = 160
FILE_ID_BYTE_LENGTH = 128


@dataclass
class BlobEntry:
    name: str           # human-readable blob name from container file
    file_path: Path     # absolute path to the GUID-named data file


@dataclass
class ContainerEntry:
    name: str
    folder: Path


# ---------------------------------------------------------------------------
# containers.index parsing
# ---------------------------------------------------------------------------

def _read_utf16_string(data: bytes, offset: int) -> tuple[str, int]:
    """Read a length-prefixed UTF-16LE string. Returns (string, new_offset)."""
    length = struct.unpack_from("<I", data, offset)[0]
    offset += 4
    raw = data[offset: offset + length * 2]
    offset += length * 2
    return raw.decode("utf-16-le").rstrip("\x00"), offset


def parse_containers_index(user_dir: Path) -> list[ContainerEntry]:
    """
    Parse the containers.index binary file in a WGS *UserID* directory
    (one level below the wgs/ root, e.g. wgs/0009000002414F5B_.../containers.index).
    Returns a list of ContainerEntry with the container name and folder path.
    """
    index_path = user_dir / "containers.index"
    if not index_path.exists():
        log.warning("containers.index not found at %s", index_path)
        return []

    data = index_path.read_bytes()
    entries: list[ContainerEntry] = []

    try:
        offset = 0
        # Skip version byte
        offset += 1
        # Read container count (4 bytes LE)
        count = struct.unpack_from("<I", data, offset)[0]
        offset += 4

        # Skip package identifier string
        _, offset = _read_utf16_string(data, offset)

        for _ in range(count):
            # Container name (UTF-16LE with length prefix)
            name, offset = _read_utf16_string(data, offset)

            # Skip a second name field (internal/alternate name)
            _, offset = _read_utf16_string(data, offset)

            # Skip a third name field
            _, offset = _read_utf16_string(data, offset)

            # Version byte
            offset += 1

            # GUID (16 bytes) — folder name in hex
            guid_bytes = data[offset: offset + 16]
            offset += 16
            folder_name = guid_bytes.hex().upper()

            # Skip FILETIME timestamps (2 × 8 bytes) and 2 × 8-byte values
            offset += 32

            # Skip container size (8 bytes)
            offset += 8

            folder_path = user_dir / folder_name
            if folder_path.exists():
                entries.append(ContainerEntry(name=name, folder=folder_path))
                log.debug("Container: %s → %s", name, folder_path)
            else:
                log.debug("Container folder not found (skipped): %s", folder_path)

    except Exception as e:
        log.warning("Error parsing containers.index: %s", e)

    return entries


# ---------------------------------------------------------------------------
# container.X file parsing
# ---------------------------------------------------------------------------

def _guid_hex_to_standard(hex_str: str) -> str:
    """
    Convert a 32-char little-endian hex string to standard GUID format.
    Reverses byte order within each GUID segment then adds hyphens.
    """
    if len(hex_str) != 32:
        return hex_str

    # Reverse byte order within each segment (little-endian to big-endian)
    def rev(s: str) -> str:
        return "".join(reversed([s[i:i+2] for i in range(0, len(s), 2)]))

    p1 = rev(hex_str[0:8])
    p2 = rev(hex_str[8:12])
    p3 = rev(hex_str[12:16])
    p4 = hex_str[16:20]   # big-endian already
    p5 = hex_str[20:32]
    return f"{p1}-{p2}-{p3}-{p4}-{p5}".upper()


def _find_container_file(folder: Path) -> Path | None:
    """Find the container.X file in a container folder."""
    for p in folder.iterdir():
        if p.name.lower().startswith("container.") and p.is_file():
            return p
    return None


def parse_container_file(container_folder: Path) -> list[BlobEntry]:
    """
    Parse the container.X binary file in a container folder.
    Returns a list of BlobEntry mapping blob names to their data file paths.
    """
    container_file = _find_container_file(container_folder)
    if not container_file:
        log.warning("No container.X file found in %s", container_folder)
        return []

    data = container_file.read_bytes()

    # Validate header
    if not data[:5] == CONTAINER_FILE_HEADER:
        log.warning("Unexpected container file header in %s", container_file)
        # Don't bail — try to parse anyway, some games have variant headers

    entries: list[BlobEntry] = []

    # Find where fixed-length entries start (skip any variable-length header)
    # Header is 8 bytes; entries follow immediately
    offset = 8
    entry_count = (len(data) - offset) // ENTRY_BYTE_LENGTH

    for i in range(entry_count):
        entry_start = offset + i * ENTRY_BYTE_LENGTH
        entry_data = data[entry_start: entry_start + ENTRY_BYTE_LENGTH]

        if len(entry_data) < ENTRY_BYTE_LENGTH:
            break

        # FileID (128 bytes): null-terminated UTF-8 blob name
        file_id = entry_data[:FILE_ID_BYTE_LENGTH]
        try:
            blob_name = file_id.split(b"\x00")[0].decode("utf-8").strip()
        except UnicodeDecodeError:
            blob_name = f"blob_{i}"

        if not blob_name:
            continue

        # First GUID (16 bytes): identifies the data file
        guid_bytes = entry_data[FILE_ID_BYTE_LENGTH: FILE_ID_BYTE_LENGTH + 16]
        guid_hex = guid_bytes.hex()
        guid_standard = _guid_hex_to_standard(guid_hex)
        guid_upper = guid_standard.replace("-", "").upper()

        # Look for matching file in the container folder (GUID-named, no extension)
        data_file = container_folder / guid_upper
        if not data_file.exists():
            # Some games store files with the hyphenated GUID
            data_file = container_folder / guid_standard
        if not data_file.exists():
            # Try case-insensitive scan
            found = _find_file_icase(container_folder, guid_upper)
            if found:
                data_file = found
            else:
                log.debug("Data file not found for blob '%s' (GUID: %s)", blob_name, guid_upper)
                continue

        entries.append(BlobEntry(name=blob_name, file_path=data_file))
        log.debug("Blob: '%s' → %s", blob_name, data_file.name)

    return entries


def _find_file_icase(folder: Path, name: str) -> Path | None:
    """Case-insensitive file search in a folder."""
    name_lower = name.lower()
    for p in folder.iterdir():
        if p.name.lower() == name_lower and p.is_file():
            return p
    return None


# ---------------------------------------------------------------------------
# High-level: get blob map for a game
# ---------------------------------------------------------------------------

def get_blob_map(wgs_path: Path) -> dict[str, Path]:
    """
    Parse the WGS directory and return a dict of {blob_name → file_path}.

    wgs_path is the wgs/ root (e.g. SystemAppData/wgs/).
    Under it are one or more <UserID> subfolders, each containing containers.index
    and <ContainerFolder>/ subdirectories with the actual GUID-named data files.

    Falls back to listing all GUID files if container parsing fails.
    """
    # Find UserID subfolders (one level below wgs_path)
    try:
        user_dirs = sorted(
            [p for p in wgs_path.iterdir() if p.is_dir()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError as e:
        log.warning("Cannot list wgs directory %s: %s", wgs_path, e)
        return {}

    # Try to parse containers.index from each UserID folder (newest first)
    for user_dir in user_dirs:
        containers = parse_containers_index(user_dir)
        if not containers:
            continue
        containers.sort(key=lambda c: c.folder.stat().st_mtime, reverse=True)
        blobs = parse_container_file(containers[0].folder)
        if blobs:
            log.debug("Parsed %d blobs from %s", len(blobs), user_dir.name)
            return {b.name: b.file_path for b in blobs}
        log.warning("Container file parsing yielded no blobs in %s; trying next", user_dir.name)

    log.warning("Container parsing failed for all UserID dirs; falling back to raw scan")

    # Fallback: collect all non-container files two levels deep (UserID/ContainerFolder/GUID)
    blob_map: dict[str, Path] = {}
    for user_dir in user_dirs:
        try:
            for container_dir in user_dir.iterdir():
                if not container_dir.is_dir():
                    continue
                for f in container_dir.iterdir():
                    if f.is_file() and not f.name.lower().startswith("container."):
                        blob_map[f.name] = f
        except OSError:
            continue
    return blob_map
