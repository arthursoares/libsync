"""Fuzzy-match classifier: folder metadata → library album candidates."""
from pathlib import Path

from backend.services.scan import (
    FolderMeta,
    LibraryIndex,
    classify,
    build_library_index,
)


def _folder(tmp_path, name="Album") -> Path:
    p = tmp_path / name
    p.mkdir()
    return p


def _library(rows: list[dict]) -> LibraryIndex:
    return build_library_index(rows)


def test_exact_match_auto(tmp_path):
    meta = FolderMeta(
        folder=_folder(tmp_path),
        artist="The Beatles", album="Abbey Road",
        bit_depth=24, sample_rate=96.0, track_count=17, source="tags",
    )
    idx = _library([
        {"id": 1, "source": "qobuz", "artist": "The Beatles",
         "title": "Abbey Road", "bit_depth": 24, "sample_rate": 96.0},
    ])
    result = classify(meta, idx)
    assert result.kind == "auto_match"
    assert result.album_id == 1
    assert result.reason == "exact"


def test_bit_depth_mismatch_goes_to_review(tmp_path):
    meta = FolderMeta(
        folder=_folder(tmp_path),
        artist="The Beatles", album="Abbey Road",
        bit_depth=16, sample_rate=44.1, track_count=17, source="tags",
    )
    idx = _library([
        {"id": 1, "source": "qobuz", "artist": "The Beatles",
         "title": "Abbey Road", "bit_depth": 24, "sample_rate": 96.0},
    ])
    result = classify(meta, idx)
    assert result.kind == "review"
    assert result.candidates[0].album_id == 1
    assert "bit_depth_mismatch" in result.candidates[0].reason


def test_unknown_bit_depth_allows_auto_match(tmp_path):
    meta = FolderMeta(
        folder=_folder(tmp_path),
        artist="Daft Punk", album="Random Access Memories",
        bit_depth=None, sample_rate=None, track_count=13, source="tags",
    )
    idx = _library([
        {"id": 2, "source": "qobuz", "artist": "Daft Punk",
         "title": "Random Access Memories", "bit_depth": 24, "sample_rate": 44.1},
    ])
    assert classify(meta, idx).kind == "auto_match"


def test_multiple_candidates_goes_to_review(tmp_path):
    # Library has two rows that normalize to the same key (same artist/album
    # across Qobuz and Tidal — common after syncing both services).
    meta = FolderMeta(
        folder=_folder(tmp_path),
        artist="Radiohead", album="In Rainbows",
        bit_depth=24, sample_rate=96.0, track_count=10, source="tags",
    )
    idx = _library([
        {"id": 3, "source": "qobuz", "artist": "Radiohead",
         "title": "In Rainbows", "bit_depth": 24, "sample_rate": 96.0},
        {"id": 4, "source": "tidal", "artist": "Radiohead",
         "title": "In Rainbows", "bit_depth": 24, "sample_rate": 44.1},
    ])
    result = classify(meta, idx)
    assert result.kind == "review"
    assert {c.album_id for c in result.candidates} == {3, 4}


def test_no_match_goes_to_unmatched(tmp_path):
    meta = FolderMeta(
        folder=_folder(tmp_path),
        artist="Nobody", album="Unknown",
        bit_depth=16, sample_rate=44.1, track_count=5, source="tags",
    )
    idx = _library([
        {"id": 5, "source": "qobuz", "artist": "Somebody",
         "title": "Something", "bit_depth": 16, "sample_rate": 44.1},
    ])
    assert classify(meta, idx).kind == "unmatched"


def test_missing_artist_matches_by_album_only_to_review(tmp_path):
    meta = FolderMeta(
        folder=_folder(tmp_path),
        artist="", album="Abbey Road",
        bit_depth=24, sample_rate=96.0, track_count=17, source="folder_name",
    )
    idx = _library([
        {"id": 1, "source": "qobuz", "artist": "The Beatles",
         "title": "Abbey Road", "bit_depth": 24, "sample_rate": 96.0},
    ])
    result = classify(meta, idx)
    assert result.kind == "review"
    assert result.candidates[0].album_id == 1
    assert "missing_artist" in result.candidates[0].reason
