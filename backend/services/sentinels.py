"""First-party discovery and safe reconciliation of download sentinels."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import mutagen

from .scan import mark_album_downloaded
from .tracks import resolve_album_track_ids

logger = logging.getLogger("streamrip")

SENTINEL_FILENAME = ".streamrip.json"
SUPPORTED_SOURCES = {"qobuz", "tidal"}
_AUDIO_EXTENSIONS = {
    ".aif",
    ".aiff",
    ".alac",
    ".ape",
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".opus",
    ".wav",
}


class SentinelValidationError(ValueError):
    """A sentinel or its local album folder is unsafe or incomplete."""


@dataclass(frozen=True)
class SentinelRecord:
    folder: Path
    payload: dict


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def discover_sentinels(
    download_root: str, *, max_depth: int = 3
) -> tuple[list[SentinelRecord], list[dict], int]:
    """Return parsed sentinels with their actual containing folders.

    Symlink directories are never followed. Malformed or unsafe entries are
    reported individually so one bad folder cannot abort the scan.
    """
    try:
        root = Path(download_root).resolve(strict=True)
    except FileNotFoundError:
        return [], [], 0
    except OSError as error:
        return [], [{"folder": str(download_root), "error": str(error)}], 0
    if not root.is_dir():
        return [], [], 0

    records: list[SentinelRecord] = []
    failures: list[dict] = []
    scanned = 0

    def walk_error(error: OSError) -> None:
        failures.append({"folder": error.filename or str(root), "error": str(error)})

    for current, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        folder = Path(current)
        try:
            actual_folder = folder.resolve(strict=True)
            depth = len(actual_folder.relative_to(root).parts)
        except (OSError, ValueError) as error:
            dirs[:] = []
            failures.append({"folder": str(folder), "error": str(error)})
            continue

        kept_dirs = []
        for name in dirs:
            candidate = folder / name
            if candidate.is_symlink():
                try:
                    target = candidate.resolve(strict=True)
                except OSError as error:
                    failures.append({"folder": str(candidate), "error": str(error)})
                    scanned += 1
                    continue
                if not _inside(target, root):
                    failures.append(
                        {
                            "folder": str(candidate),
                            "error": "Refusing symlink escape outside downloads root",
                        }
                    )
                    scanned += 1
                continue
            kept_dirs.append(name)
        dirs[:] = kept_dirs if depth < max_depth else []

        if SENTINEL_FILENAME not in files:
            continue
        scanned += 1
        sentinel = folder / SENTINEL_FILENAME
        if sentinel.is_symlink():
            failures.append(
                {
                    "folder": str(actual_folder),
                    "error": "Refusing symlink sentinel",
                }
            )
            continue
        try:
            actual_sentinel = sentinel.resolve(strict=True)
            if not _inside(actual_sentinel, root):
                raise SentinelValidationError(
                    "Sentinel resolves outside downloads root"
                )
            with actual_sentinel.open(encoding="utf-8") as file:
                payload = json.load(file)
            if not isinstance(payload, dict):
                raise SentinelValidationError("Sentinel JSON must be an object")
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            SentinelValidationError,
        ) as error:
            failures.append(
                {
                    "folder": str(actual_folder),
                    "error": f"Invalid sentinel JSON: {error}",
                }
            )
            continue
        records.append(SentinelRecord(folder=actual_folder, payload=payload))

    return records, failures, scanned


def sentinel_identity(payload: dict) -> tuple[str, str]:
    """Validate and normalize a sentinel source and album identity."""
    if "source" not in payload:
        source = "qobuz"
    else:
        raw_source = payload["source"]
        if not isinstance(raw_source, str):
            raise SentinelValidationError("Unsupported sentinel source")
        source = raw_source.strip().lower()
    if source not in SUPPORTED_SOURCES:
        raise SentinelValidationError(f"Unsupported sentinel source: {source or '?'}")

    raw_album_id = payload.get("album_id")
    if isinstance(raw_album_id, bool) or not isinstance(raw_album_id, (str, int)):
        raise SentinelValidationError("Sentinel album_id is missing or invalid")
    album_id = str(raw_album_id).strip()
    if not album_id:
        raise SentinelValidationError("Sentinel album_id is missing or invalid")
    return source, album_id


def sentinel_downloaded_at(payload: dict) -> str:
    """Preserve valid sentinel timestamps; otherwise use current UTC time."""
    raw = payload.get("downloaded_at")
    if isinstance(raw, str):
        value = raw.strip()
        try:
            datetime.fromisoformat(value)
        except ValueError:
            pass
        else:
            return value
    return datetime.now(timezone.utc).isoformat()


def _optional_positive_count(payload: dict, key: str) -> int | None:
    if key not in payload:
        return None
    raw = payload[key]
    if isinstance(raw, bool):
        raise SentinelValidationError(f"Sentinel {key} is invalid")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise SentinelValidationError(f"Sentinel {key} is invalid") from None
    if value <= 0:
        raise SentinelValidationError(f"Sentinel {key} is invalid")
    return value


def _audio_files(folder: Path, *, max_depth: int = 2) -> list[Path]:
    files: list[Path] = []
    for current, dirs, names in os.walk(folder, followlinks=False):
        current_path = Path(current)
        depth = len(current_path.relative_to(folder).parts)
        kept_dirs = []
        for name in dirs:
            candidate = current_path / name
            if candidate.is_symlink():
                try:
                    target = candidate.resolve(strict=True)
                except OSError as error:
                    raise SentinelValidationError(str(error)) from error
                if not _inside(target, folder):
                    raise SentinelValidationError(
                        "Album contains a symlink escape outside its folder"
                    )
                continue
            kept_dirs.append(name)
        dirs[:] = kept_dirs if depth < max_depth else []

        for name in names:
            path = current_path / name
            if path.suffix.lower() not in _AUDIO_EXTENSIONS:
                continue
            if path.is_symlink():
                raise SentinelValidationError("Album contains a symlink audio file")
            files.append(path)
    return sorted(files)


def _tag_identity(path: Path) -> str | None:
    try:
        audio = mutagen.File(str(path), easy=True)
    except Exception:
        return None
    tags = getattr(audio, "tags", None) if audio is not None else None
    if tags is None:
        return None
    raw = tags.get("isrc")
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if raw is None:
        return None
    value = str(raw).strip().casefold()
    return value or None


def validate_local_album(
    folder: Path,
    payload: dict,
    track_ids: tuple[str, ...],
    catalog_tracks: list[dict],
) -> None:
    """Require a complete, non-ambiguous local album before promotion."""
    expected = len(track_ids)
    sentinel_count = _optional_positive_count(payload, "tracks_count")
    if sentinel_count is not None and sentinel_count != expected:
        raise SentinelValidationError(
            f"Sentinel track count is {sentinel_count}; catalog expects {expected}"
        )
    downloaded_count = _optional_positive_count(payload, "tracks_downloaded")
    if downloaded_count is not None and downloaded_count != expected:
        raise SentinelValidationError(
            f"Sentinel reports {downloaded_count} downloaded tracks; expected {expected}"
        )

    audio_files = _audio_files(folder)
    if len(audio_files) != expected:
        raise SentinelValidationError(
            f"Incomplete local album: expected {expected} audio files, "
            f"found {len(audio_files)}"
        )
    file_identities = []
    for path in audio_files:
        stat = path.stat(follow_symlinks=False)
        file_identities.append((stat.st_dev, stat.st_ino))
    if len(set(file_identities)) != len(file_identities):
        raise SentinelValidationError("Album contains duplicate linked audio files")

    sentinel_tracks = payload.get("tracks")
    if sentinel_tracks is not None:
        if not isinstance(sentinel_tracks, list):
            raise SentinelValidationError("Sentinel tracks must be a list")
        successful_ids = []
        for track in sentinel_tracks:
            if not isinstance(track, dict) or track.get("success") is not True:
                continue
            raw_id = track.get("id")
            if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)):
                raise SentinelValidationError("Sentinel track identity is invalid")
            successful_ids.append(str(raw_id).strip())
        if len(successful_ids) != expected or len(set(successful_ids)) != expected:
            raise SentinelValidationError(
                "Sentinel track identities are partial or duplicated"
            )
        if set(successful_ids) != set(track_ids):
            raise SentinelValidationError(
                "Sentinel track identities do not match the current catalog"
            )

    local_isrcs = [_tag_identity(path) for path in audio_files]
    tagged_isrcs = [value for value in local_isrcs if value is not None]
    if tagged_isrcs:
        if len(tagged_isrcs) != expected:
            raise SentinelValidationError("Local track identity tags are incomplete")
        current_ids = set(track_ids)
        catalog_isrcs = [
            str(track["isrc"]).strip().casefold()
            for track in catalog_tracks
            if track["source_track_id"] in current_ids and track.get("isrc")
        ]
        if len(catalog_isrcs) == expected and Counter(tagged_isrcs) != Counter(
            catalog_isrcs
        ):
            raise SentinelValidationError(
                "Local track identity tags do not match the current catalog"
            )


def _metadata_text(payload: dict, key: str) -> str:
    value = payload.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else "Unknown"


async def reconcile_sentinels(
    db,
    clients: dict,
    event_bus,
    *,
    download_root: str,
    dedup_db_dir: str,
) -> dict:
    """Discover and reconcile valid complete sentinel albums independently."""
    records, failures, scanned = await asyncio.to_thread(
        discover_sentinels, download_root
    )
    reconciled = 0
    successes: dict[str, dict[str, int]] = defaultdict(
        lambda: {"new_count": 0, "total": 0}
    )

    for record in records:
        source: str | None = None
        source_album_id: str | None = None
        created = False
        try:
            source, source_album_id = sentinel_identity(record.payload)
            downloaded_at = sentinel_downloaded_at(record.payload)
            existing = await asyncio.to_thread(
                db.get_album_by_source_id, source, source_album_id
            )
            if existing is None:
                track_count = _optional_positive_count(record.payload, "tracks_count")
                album_id = await asyncio.to_thread(
                    db.upsert_album,
                    source,
                    source_album_id,
                    _metadata_text(record.payload, "title"),
                    _metadata_text(record.payload, "artist"),
                    track_count=track_count,
                    added_to_library_at=downloaded_at,
                )
                created = True
            else:
                album_id = existing["id"]

            track_ids = await resolve_album_track_ids(db, clients, album_id)
            catalog_tracks = await asyncio.to_thread(db.get_tracks, album_id)
            await asyncio.to_thread(
                validate_local_album,
                record.folder,
                record.payload,
                track_ids,
                catalog_tracks,
            )
            await asyncio.to_thread(
                mark_album_downloaded,
                db,
                album_id,
                local_folder_path=str(record.folder),
                dedup_db_dir=dedup_db_dir,
                track_ids=track_ids,
                sentinel_write_enabled=False,
                downloaded_at=downloaded_at,
            )
        except Exception as error:
            logger.exception("Could not reconcile sentinel in %s", record.folder)
            failure = {"folder": str(record.folder), "error": str(error)}
            if source is not None:
                failure["source"] = source
            if source_album_id is not None:
                failure["album_id"] = source_album_id
            failures.append(failure)
            continue

        reconciled += 1
        successes[source]["total"] += 1
        if created:
            successes[source]["new_count"] += 1
        await event_bus.publish(
            "album_status_changed", {"album_id": album_id, "status": "complete"}
        )

    for source, counts in successes.items():
        await event_bus.publish("library_updated", {"source": source, **counts})

    return {"scanned": scanned, "reconciled": reconciled, "failures": failures}
