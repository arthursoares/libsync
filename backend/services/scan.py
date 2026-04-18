"""Fuzzy folder-to-library matching + mark-as-downloaded primitives.

See docs/superpowers/specs/2026-04-18-library-scan-fuzzy-match-design.md
for the design rationale.
"""
from __future__ import annotations

import re
import unicodedata

_PAREN_SUFFIX_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*$")
_LEADING_THE_RE = re.compile(r"^the\s+", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")


def normalize(value: str | None) -> str:
    """Fold an artist or album name into a stable comparison key.

    Steps: casefold, strip trailing parens/brackets (repeatedly, so
    "X (Deluxe) (Remastered)" collapses), NFKD-normalize and drop
    combining marks (diacritics), strip one leading "The ", collapse
    whitespace.
    """
    if not value:
        return ""

    s = value.casefold()
    while True:
        stripped = _PAREN_SUFFIX_RE.sub("", s)
        if stripped == s:
            break
        s = stripped

    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))

    s = _LEADING_THE_RE.sub("", s, count=1)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    return s


from dataclasses import dataclass
from pathlib import Path

import mutagen

_AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".ape", ".alac", ".aif", ".aiff"}


@dataclass(frozen=True)
class FolderMeta:
    """Metadata extracted from an album folder."""
    folder: Path
    artist: str
    album: str
    bit_depth: int | None
    sample_rate: float | None
    track_count: int
    source: str  # "tags" or "folder_name"


def _audio_files(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() in _AUDIO_EXTS)


def _tag_value(tags, key: str) -> str | None:
    if tags is None:
        return None
    val = tags.get(key)
    if val is None:
        return None
    if isinstance(val, list):
        val = val[0] if val else None
    return str(val) if val is not None else None


def _parse_folder_name(name: str) -> tuple[str | None, str]:
    """Split folder name into (artist, album).

    Heuristic: "Artist - Album" → (Artist, Album); otherwise
    (None, entire_name).
    """
    if " - " in name:
        artist, album = name.split(" - ", 1)
        return artist.strip(), album.strip()
    return None, name.strip()


def read_folder_metadata(folder: Path) -> FolderMeta | None:
    """Extract artist/album/bit_depth/sample_rate from a folder.

    Returns None if the folder has no audio files.
    """
    audio = _audio_files(folder)
    if not audio:
        return None

    artist: str | None = None
    album: str | None = None
    bit_depth: int | None = None
    sample_rate: float | None = None
    source = "folder_name"

    try:
        f = mutagen.File(str(audio[0]), easy=True)
    except Exception:
        f = None

    if f is not None:
        tags = getattr(f, "tags", None)
        artist = _tag_value(tags, "albumartist") or _tag_value(tags, "artist")
        album = _tag_value(tags, "album")

        info = getattr(f, "info", None)
        if info is not None:
            bps = getattr(info, "bits_per_sample", None)
            if isinstance(bps, int) and bps > 0:
                bit_depth = bps
            sr = getattr(info, "sample_rate", None)
            if isinstance(sr, (int, float)) and sr > 0:
                sample_rate = round(sr / 1000, 1)

        if artist and album:
            source = "tags"

    if not album:
        parsed_artist, parsed_album = _parse_folder_name(folder.name)
        album = parsed_album
        if not artist:
            artist = parsed_artist

    if not album:
        return None

    return FolderMeta(
        folder=folder,
        artist=artist or "",
        album=album,
        bit_depth=bit_depth,
        sample_rate=sample_rate,
        track_count=len(audio),
        source=source,
    )
