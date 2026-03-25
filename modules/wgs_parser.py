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
import re
import struct
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# UserID folders look like: 0009000002414F5B_000000000000000000000000713AFEB2
_USERID_RE = re.compile(r'^[0-9A-Fa-f]{8,}_[0-9A-Fa-f]{16,}$')

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


def _mixed_endian_guid_to_folder(b: bytes) -> str:
    """
    Convert 16 raw GUID bytes (Windows mixed-endian: Data1 LE, Data2 LE, Data3 LE, Data4 BE)
    into the uppercase hex folder-name string used by WGS.

    Example: bytes [15 9B 4F 33 9B E4 1A 4E BA AD 52 62 60 4A 8D A6]
             → "334F9B15E49B4E1ABAAD5262604A8DA6"
    """
    return (
        f"{struct.unpack_from('<I', b, 0)[0]:08X}"
        f"{struct.unpack_from('<H', b, 4)[0]:04X}"
        f"{struct.unpack_from('<H', b, 6)[0]:04X}"
        + "".join(f"{x:02X}" for x in b[8:16])
    )


def parse_containers_index(user_dir: Path) -> list[ContainerEntry]:
    """
    Parse the containers.index binary file in a WGS *UserID* directory.

    Confirmed format (version 14, from hex analysis of real saves):
      Header:
        4 bytes  version (LE uint32, e.g. 14)
        4 bytes  container count (LE uint32)
        4 bytes  unknown
      Package name string (UTF-16LE, 4-byte length prefix)
      8 bytes  FILETIME
      4 bytes  unknown
      For each container:
        GUID string (UTF-16LE, length-prefixed, e.g. "a2f9b97f-8bdc-42a0-b164-5c9d986e79b8")
        8 bytes  unknown
        container name string (UTF-16LE)  e.g. "GameSaves"
        container name string again
        internal ID string (UTF-16LE)     e.g. '"0x8DE89BC69E7FF72"'
        1 byte   unknown
        4 bytes  unknown
        16 bytes folder GUID (Windows mixed-endian)  ← this gives the folder name
        8 bytes  FILETIME
        8 bytes  unknown (zeros)
        8 bytes  container size (LE uint64)
    """
    index_path = user_dir / "containers.index"
    if not index_path.exists():
        log.warning("containers.index not found at %s", index_path)
        return []

    data = index_path.read_bytes()
    entries: list[ContainerEntry] = []

    try:
        offset = 0
        # 4-byte version
        offset += 4
        # 4-byte container count
        count = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        # 4-byte unknown
        offset += 4

        # Package name (UTF-16LE, length-prefixed)
        _, offset = _read_utf16_string(data, offset)

        # FILETIME (8 bytes)
        offset += 8
        # 4-byte unknown
        offset += 4

        for _ in range(count):
            # GUID as UTF-16LE string (e.g. "a2f9b97f-8bdc-42a0-b164-5c9d986e79b8")
            _, offset = _read_utf16_string(data, offset)
            # 8 bytes unknown
            offset += 8

            # Container name (x2)
            name, offset = _read_utf16_string(data, offset)
            _, offset = _read_utf16_string(data, offset)

            # Internal ID string (e.g. '"0x8DE89BC69E7FF72"')
            _, offset = _read_utf16_string(data, offset)

            # 1 byte + 4 bytes unknown
            offset += 5

            # 16-byte folder GUID (Windows mixed-endian)
            if offset + 16 > len(data):
                break
            folder_name = _mixed_endian_guid_to_folder(data[offset:offset + 16])
            offset += 16

            # FILETIME (8) + unknown (8) + size (8) = 24 bytes
            offset += 24

            folder_path = user_dir / folder_name
            if folder_path.exists():
                entries.append(ContainerEntry(name=name, folder=folder_path))
                log.debug("Container: %s → %s", name, folder_path)
            else:
                log.debug("Container folder not found: %s (GUID: %s)", folder_path, folder_name)

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

    Confirmed format (from hex analysis):
      0x000: version (4 bytes LE, e.g. 4)
      0x004: entry count (4 bytes LE)
      For each entry (160 bytes):
        0x000-0x07F: blob name, UTF-16LE null-padded to 128 bytes
        0x080-0x08F: file GUID (16 bytes, Windows mixed-endian)
        0x090-0x09F: second GUID (16 bytes, usually same as first)

    Returns a list of BlobEntry mapping blob names to their data file paths.
    """
    container_file = _find_container_file(container_folder)
    if not container_file:
        log.warning("No container.X file found in %s", container_folder)
        return []

    data = container_file.read_bytes()

    if len(data) < 8:
        log.warning("Container file too small: %s", container_file)
        return []

    # Header: version (4 bytes) + entry count (4 bytes)
    entry_count = struct.unpack_from("<I", data, 4)[0]
    offset = 8

    if entry_count == 0 or entry_count > 10000:
        log.warning("Implausible entry count %d in %s", entry_count, container_file)
        return []

    entries: list[BlobEntry] = []

    for i in range(entry_count):
        entry_start = offset + i * ENTRY_BYTE_LENGTH
        entry_data = data[entry_start: entry_start + ENTRY_BYTE_LENGTH]

        if len(entry_data) < ENTRY_BYTE_LENGTH:
            break

        # Blob name: UTF-16LE, null-padded to FILE_ID_BYTE_LENGTH bytes
        name_bytes = entry_data[:FILE_ID_BYTE_LENGTH]
        try:
            blob_name = name_bytes.decode("utf-16-le").rstrip("\x00").strip()
        except UnicodeDecodeError:
            blob_name = f"blob_{i}"

        if not blob_name:
            continue

        # File GUID (16 bytes, Windows mixed-endian) → uppercase folder name
        guid_bytes = entry_data[FILE_ID_BYTE_LENGTH: FILE_ID_BYTE_LENGTH + 16]
        guid_upper = _mixed_endian_guid_to_folder(guid_bytes)

        data_file = container_folder / guid_upper
        if not data_file.exists():
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
    # Find UserID subfolders (one level below wgs_path).
    # Only consider folders whose names match the expected UserID pattern
    # (e.g. 0009000002414F5B_000000000000000000000000713AFEB2) — ignores
    # stray files/folders like 't' that some games leave in the wgs directory.
    try:
        user_dirs = sorted(
            [p for p in wgs_path.iterdir() if p.is_dir() and _USERID_RE.match(p.name)],
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
