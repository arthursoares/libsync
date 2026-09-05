"""Fuzzy folder-to-library matching + mark-as-downloaded primitives.

See docs/superpowers/specs/2026-04-18-library-scan-fuzzy-match-design.md
for the design rationale.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import mutagen

from ..models.database import AlbumNotFoundError
from .tracks import resolve_album_track_ids

logger = logging.getLogger("streamrip")

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


_AUDIO_EXTS = {
    ".flac",
    ".mp3",
    ".m4a",
    ".ogg",
    ".opus",
    ".wav",
    ".ape",
    ".alac",
    ".aif",
    ".aiff",
}


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
    return sorted(
        p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in _AUDIO_EXTS
    )


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


def _track_count_matches(local: int | None, library: int | None) -> bool:
    """Unknown (None or 0) on either side is treated as 'compatible'.

    A folder holding a different number of audio files than the library
    album is usually an interrupted download. Auto-matching it would flip
    the album to complete AND write every track ID into the dedup DB, so
    the missing tracks could never be fetched again — those cases must go
    to review instead.
    """
    if not local or not library:
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

    # Partition candidates by bit-depth AND track-count compatibility.
    compatible: list[dict] = []
    for album in candidates:
        if _bit_depth_matches(
            meta.bit_depth, album.get("bit_depth")
        ) and _track_count_matches(meta.track_count, album.get("track_count")):
            compatible.append(album)

    # Auto-match only when we have exactly one candidate overall AND exactly
    # one compatible candidate AND the folder had a reliable artist (so
    # album-only fallback matches always need review). Requiring the original
    # pool to also be a singleton prevents silently auto-matching when the
    # compatibility filter happened to narrow multiple candidates (e.g. Qobuz
    # + Tidal copies of the same album) down to one.
    if len(candidates) == 1 and len(compatible) == 1 and norm_artist:
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
        if not _track_count_matches(meta.track_count, album.get("track_count")):
            reasons.append(
                f"track_count_mismatch: local={meta.track_count} "
                f"library={album.get('track_count')}"
            )
        if len(candidates) > 1 and not reasons:
            reasons.append("multiple_candidates")
        if not reasons:
            reasons.append("ambiguous")
        review_candidates.append(
            Candidate(
                album_id=album["id"],
                source=album["source"],
                artist=album["artist"],
                title=album["title"],
                score=0.9 if norm_artist else 0.6,
                reason="; ".join(reasons),
            )
        )

    return MatchResult(
        kind="review",
        candidates=tuple(review_candidates),
    )


def _dedup_db_path(source: str, dedup_db_dir: str) -> str:
    fname = "downloads.db" if source == "qobuz" else f"downloads-{source}.db"
    return os.path.join(dedup_db_dir, fname)


def _sentinel_payload(album: dict, downloaded_at: str) -> dict:
    return {
        "source": album["source"],
        "album_id": album["source_album_id"],
        "title": album["title"],
        "artist": album["artist"],
        "tracks_count": album.get("track_count"),
        "downloaded_at": downloaded_at,
    }


def _write_sentinel(folder: str, payload: dict) -> bool:
    """Best-effort sentinel write. Returns True on success."""
    try:
        with open(os.path.join(folder, ".streamrip.json"), "w") as f:
            json.dump(payload, f)
        return True
    except OSError as e:
        logger.warning("scan: could not write sentinel to %s: %s", folder, e)
        return False


def _remove_sentinel(folder: str | None) -> None:
    if not folder:
        return
    try:
        os.remove(os.path.join(folder, ".streamrip.json"))
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("scan: could not remove sentinel at %s: %s", folder, e)


def mark_album_downloaded(
    db,
    album_id: int,
    *,
    local_folder_path: str | None,
    dedup_db_dir: str,
    track_ids: tuple[str, ...],
    sentinel_write_enabled: bool = True,
    now: datetime | None = None,
    downloaded_at: str | None = None,
) -> None:
    """Mark an album complete in DB, dedup DB, and optionally on disk.

    Idempotent — calling repeatedly is safe. Sentinel writes degrade
    gracefully on read-only mounts.
    """
    if not track_ids:
        raise ValueError("A complete non-empty track identity set is required")

    album_hint = db.get_album(album_id)
    if album_hint is None:
        raise AlbumNotFoundError(f"Album {album_id} not found")
    downloaded_at = downloaded_at or (now or datetime.now()).isoformat()
    album = db.apply_album_download_state(
        album_id,
        True,
        track_ids,
        _dedup_db_path(album_hint["source"], dedup_db_dir),
        downloaded_at,
        local_folder_path,
    )

    if local_folder_path and sentinel_write_enabled:
        _write_sentinel(
            local_folder_path,
            _sentinel_payload(album, downloaded_at),
        )


def unmark_album_downloaded(
    db,
    album_id: int,
    *,
    dedup_db_dir: str,
    track_ids: tuple[str, ...],
) -> None:
    if not track_ids:
        raise ValueError("A complete non-empty track identity set is required")

    album = db.get_album(album_id)
    if album is None:
        raise AlbumNotFoundError(f"Album {album_id} not found")
    old_album = db.apply_album_download_state(
        album_id,
        False,
        track_ids,
        _dedup_db_path(album["source"], dedup_db_dir),
        None,
        None,
    )
    _remove_sentinel(old_album.get("local_folder_path"))


def _find_album_folders(root: Path, max_depth: int = 3) -> tuple[list[Path], list[str]]:
    """Yield folders that look like albums (contain audio files directly).

    Returns (album_folders, skipped_dirs). Unreadable directories are
    logged and recorded in skipped_dirs rather than aborting the walk.

    Walks the tree down to `max_depth` levels. A folder with audio files
    directly is treated as an album — its subfolders are NOT recursed into
    (prevents double-counting parent Album folders vs Disc 1/, Disc 2/
    children). Multi-disc albums with Disc N/ subdirectories will produce
    one candidate per disc folder; the matcher will surface duplicates for
    review, which is an acceptable trade-off for now.
    """
    results: list[Path] = []
    skipped: list[str] = []

    def walk(folder: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            children = list(folder.iterdir())
        except OSError as e:
            logger.warning("scan: skipping unreadable folder %s: %s", folder, e)
            skipped.append(str(folder))
            return

        has_audio = any(
            p.is_file() and not p.is_symlink() and p.suffix.lower() in _AUDIO_EXTS
            for p in children
        )
        if has_audio:
            results.append(folder)
            return
        for child in sorted(children):
            # Skip symlinks — they can escape the configured downloads root.
            if child.is_symlink():
                skipped.append(str(child))
                continue
            if child.is_dir() and not child.name.startswith("."):
                walk(child, depth + 1)

    walk(root, 0)
    return sorted(results), skipped


def _inspect_folder(folder: Path) -> tuple[bool, FolderMeta | None]:
    """Sentinel probe + tag read, together, for one album folder.

    Both are blocking file-system calls, so they share a single worker
    thread hop. Returns (has_sentinel, meta); ``meta`` is None when the
    folder is sentineled or holds no readable audio.
    """
    if (folder / ".streamrip.json").exists():
        return True, None
    return False, read_folder_metadata(folder)


async def run_scan(
    db,
    *,
    clients: dict,
    download_path: str,
    dedup_db_dir: str,
    event_bus,
    sentinel_write_enabled: bool = True,
) -> dict:
    """Walk the download path, classify each album folder, auto-mark
    exact matches, and return a review/unmatched report.

    Emits ``scan_progress`` after each folder and ``scan_complete`` at
    the end via the provided event bus. File-system work runs in a
    worker thread so the event loop stays responsive.
    """
    index = build_library_index(db.get_all_albums_for_index())

    root = Path(download_path)
    if not root.is_dir():  # noqa: ASYNC240 — quick stat, no blocking I/O
        return {
            "status": "complete",
            "scanned": 0,
            "sentinel_skipped": 0,
            "skipped_dirs": [],
            "auto_matched": [],
            "review": [],
            "unmatched": [],
            "failed": [],
        }

    folders, skipped_dirs = await asyncio.to_thread(_find_album_folders, root)
    total = len(folders)

    auto_matched: list[dict] = []
    review: list[dict] = []
    unmatched: list[str] = []
    failed: list[dict] = []
    sentinel_skipped = 0

    for i, folder in enumerate(folders, start=1):
        has_sentinel, meta = await asyncio.to_thread(_inspect_folder, folder)
        if has_sentinel:
            sentinel_skipped += 1
            await event_bus.publish("scan_progress", {"scanned": i, "total": total})
            continue

        if meta is None:
            await event_bus.publish("scan_progress", {"scanned": i, "total": total})
            continue

        result = classify(meta, index)

        if result.kind == "auto_match":
            # One unlucky folder (a locked dedup DB, an album deleted
            # mid-scan) must not unwind the whole job and discard every
            # album marked so far — record it and keep going.
            try:
                album_id = result.album_id
                if album_id is None:
                    raise ValueError("Auto-match did not include an album ID")
                track_ids = await resolve_album_track_ids(db, clients, album_id)
                if meta.track_count != len(track_ids):
                    album = db.get_album(album_id)
                    if album is None:
                        raise ValueError(f"Album {album_id} not found")
                    review.append(
                        {
                            "folder": str(folder),
                            "local_bit_depth": meta.bit_depth,
                            "local_sample_rate": meta.sample_rate,
                            "candidates": [
                                {
                                    "album_id": album_id,
                                    "source": album["source"],
                                    "artist": album["artist"],
                                    "title": album["title"],
                                    "score": 0.9,
                                    "reason": (
                                        "track_count_mismatch: "
                                        f"local={meta.track_count} "
                                        f"library={len(track_ids)}"
                                    ),
                                }
                            ],
                        }
                    )
                    await event_bus.publish(
                        "scan_progress", {"scanned": i, "total": total}
                    )
                    continue
                await asyncio.to_thread(
                    mark_album_downloaded,
                    db,
                    album_id,
                    local_folder_path=str(folder),
                    dedup_db_dir=dedup_db_dir,
                    track_ids=track_ids,
                    sentinel_write_enabled=sentinel_write_enabled,
                )
            except Exception as e:
                logger.exception(
                    "scan: could not mark album %s for %s", result.album_id, folder
                )
                failed.append(
                    {
                        "folder": str(folder),
                        "album_id": result.album_id,
                        "error": str(e),
                    }
                )
            else:
                auto_matched.append(
                    {
                        "album_id": result.album_id,
                        "folder": str(folder),
                        "reason": result.reason,
                    }
                )
        elif result.kind == "review":
            review.append(
                {
                    "folder": str(folder),
                    "local_bit_depth": meta.bit_depth,
                    "local_sample_rate": meta.sample_rate,
                    "candidates": [
                        {
                            "album_id": c.album_id,
                            "source": c.source,
                            "artist": c.artist,
                            "title": c.title,
                            "score": c.score,
                            "reason": c.reason,
                        }
                        for c in result.candidates
                    ],
                }
            )
        else:
            unmatched.append(str(folder))

        await event_bus.publish("scan_progress", {"scanned": i, "total": total})

    payload = {
        "status": "complete",
        "scanned": total,
        "sentinel_skipped": sentinel_skipped,
        "skipped_dirs": skipped_dirs,
        "auto_matched": auto_matched,
        "review": review,
        "unmatched": unmatched,
        "failed": failed,
    }
    await event_bus.publish("scan_complete", payload)
    return payload
