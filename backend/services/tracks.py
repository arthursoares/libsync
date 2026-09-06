"""Strict catalog-backed track identity resolution and normalization."""

from __future__ import annotations

from ..models.database import AppDatabase
from .tasks import run_thread_write


class TrackIdentityError(RuntimeError):
    """A complete authoritative album track list could not be resolved."""

    status_code = 502


class TrackClientUnavailableError(TrackIdentityError):
    """The album source has no connected client."""

    status_code = 503


def _authoritative_track_count(source: str, album) -> int:
    if source == "qobuz":
        value = getattr(album, "tracks_count", None)
    elif source == "tidal":
        value = getattr(album, "number_of_tracks", None)
    else:
        raise TrackIdentityError(f"Unsupported album source: {source}")

    if isinstance(value, bool):
        raise TrackIdentityError(
            f"Catalog returned an invalid authoritative track count for {source}"
        )
    if value is None:
        raise TrackIdentityError(
            f"Catalog did not provide an authoritative track count for {source}"
        )
    try:
        count = int(value)
    except (TypeError, ValueError):
        raise TrackIdentityError(
            f"Catalog did not provide an authoritative track count for {source}"
        ) from None
    if count <= 0:
        raise TrackIdentityError(
            f"Catalog returned an empty authoritative track count for {source}"
        )
    return count


def _normalize_track(source: str, track) -> dict:
    raw_id = getattr(track, "id", None)
    if raw_id is None:
        raise TrackIdentityError("Catalog returned a track without an identity")
    track_id = str(raw_id).strip()
    if not track_id:
        raise TrackIdentityError("Catalog returned a track with an empty identity")

    if source == "qobuz":
        performer = getattr(track, "performer", None)
        artist = getattr(performer, "name", None) or "Unknown"
        disc_number = getattr(track, "disc_number", 1)
    elif source == "tidal":
        primary_artist = getattr(track, "artist", None)
        artist = getattr(primary_artist, "name", None)
        if not artist:
            artists = getattr(track, "artists", None) or []
            artist = ", ".join(a.name for a in artists if getattr(a, "name", None))
        artist = artist or "Unknown"
        disc_number = getattr(track, "volume_number", 1)
    else:
        raise TrackIdentityError(f"Unsupported album source: {source}")

    return {
        "source_track_id": track_id,
        "title": getattr(track, "title", None) or "Unknown",
        "artist": artist,
        "track_number": getattr(track, "track_number", None),
        "disc_number": disc_number,
        "duration_seconds": getattr(track, "duration", None),
        "explicit": bool(getattr(track, "explicit", False)),
        "isrc": getattr(track, "isrc", None),
    }


def normalize_catalog_tracks(source: str, album, tracks) -> tuple[int, list[dict]]:
    """Normalize and validate a complete SDK album response before persistence."""
    authoritative_count = _authoritative_track_count(source, album)
    try:
        normalized = [_normalize_track(source, track) for track in tracks]
    except TypeError as error:
        raise TrackIdentityError(
            "Catalog returned an invalid track collection"
        ) from error

    if len(normalized) != authoritative_count:
        raise TrackIdentityError(
            "Catalog returned an incomplete track list: "
            f"expected {authoritative_count}, received {len(normalized)}"
        )

    track_ids = [track["source_track_id"] for track in normalized]
    if len(set(track_ids)) != len(track_ids):
        raise TrackIdentityError("Catalog returned duplicate track identities")

    return authoritative_count, normalized


async def resolve_album_track_ids(
    db: AppDatabase, clients: dict, album_id: int
) -> tuple[str, ...]:
    """Resolve, validate, and atomically cache an album's complete track list."""
    album_row = db.get_album(album_id)
    if album_row is None:
        raise TrackIdentityError(f"Album {album_id} not found")

    source = album_row["source"]
    client = clients.get(source)
    catalog = getattr(client, "catalog", None) if client is not None else None
    get_album = getattr(catalog, "get_album_with_tracks", None)
    if get_album is None:
        raise TrackClientUnavailableError(
            f"Connect {source.title()} to load this album's complete track catalog"
        )

    try:
        catalog_album, catalog_tracks = await get_album(album_row["source_album_id"])
    except Exception as error:
        raise TrackIdentityError(
            f"Could not load the complete {source.title()} track catalog: {error}"
        ) from error

    try:
        authoritative_count, normalized = normalize_catalog_tracks(
            source, catalog_album, catalog_tracks
        )
    except TrackIdentityError:
        raise
    except Exception as error:
        raise TrackIdentityError(
            f"Could not normalize the complete {source.title()} track catalog: {error}"
        ) from error

    await run_thread_write(
        db.cache_album_tracks,
        album_id,
        normalized,
        authoritative_count=authoritative_count,
        operation="album track cache write",
    )
    return tuple(track["source_track_id"] for track in normalized)
