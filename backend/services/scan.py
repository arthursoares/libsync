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


from collections import defaultdict


@dataclass(frozen=True)
class Candidate:
    album_id: int
    source: str
    artist: str
    title: str
    score: float
    reason: str


@dataclass(frozen=True)
class MatchResult:
    """Result of classifying one folder against the library."""
    kind: str  # "auto_match" | "review" | "unmatched"
    album_id: int | None = None  # set when kind == "auto_match"
    reason: str = ""
    candidates: tuple[Candidate, ...] = ()


@dataclass
class LibraryIndex:
    """Index of library albums for O(1) fuzzy lookup.

    `by_full_key` maps (norm_artist, norm_album) → list of album rows.
    `by_album_only` maps norm_album → list of album rows (fallback when
    the folder has no reliable artist).
    """
    by_full_key: dict[tuple[str, str], list[dict]]
    by_album_only: dict[str, list[dict]]


def build_library_index(albums: list[dict]) -> LibraryIndex:
    full: dict[tuple[str, str], list[dict]] = defaultdict(list)
    by_album: dict[str, list[dict]] = defaultdict(list)
    for a in albums:
        na = normalize(a["artist"])
        nt = normalize(a["title"])
        full[(na, nt)].append(a)
        by_album[nt].append(a)
    return LibraryIndex(dict(full), dict(by_album))


def _bit_depth_matches(local: int | None, library: int | None) -> bool:
    """Unknown on either side is treated as 'compatible'."""
    if local is None or library is None:
        return True
    return local == library


def classify(meta: FolderMeta, index: LibraryIndex) -> MatchResult:
    norm_artist = normalize(meta.artist)
    norm_album = normalize(meta.album)

    if norm_artist:
        candidates = index.by_full_key.get((norm_artist, norm_album), [])
    else:
        candidates = index.by_album_only.get(norm_album, [])

    if not candidates:
        return MatchResult(kind="unmatched")

    # Partition candidates by bit-depth compatibility.
    compatible: list[dict] = []
    for album in candidates:
        if _bit_depth_matches(meta.bit_depth, album.get("bit_depth")):
            compatible.append(album)

    # Auto-match only when we have exactly one compatible candidate AND the
    # folder had a reliable artist (so album-only fallback matches always
    # need review).
    if len(compatible) == 1 and norm_artist:
        a = compatible[0]
        return MatchResult(
            kind="auto_match",
            album_id=a["id"],
            reason="exact",
        )

    # Everything else → review. Produce Candidate entries with reasons.
    review_candidates = []
    for album in candidates:
        reasons = []
        if not norm_artist:
            reasons.append("missing_artist")
        if not _bit_depth_matches(meta.bit_depth, album.get("bit_depth")):
            reasons.append(
                f"bit_depth_mismatch: local={meta.bit_depth} library={album.get('bit_depth')}"
            )
        if len(candidates) > 1 and not reasons:
            reasons.append("multiple_candidates")
        if not reasons:
            reasons.append("ambiguous")
        review_candidates.append(Candidate(
            album_id=album["id"],
            source=album["source"],
            artist=album["artist"],
            title=album["title"],
            score=0.9 if norm_artist else 0.6,
            reason="; ".join(reasons),
        ))

    return MatchResult(
        kind="review",
        candidates=tuple(review_candidates),
    )
