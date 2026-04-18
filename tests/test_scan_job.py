"""End-to-end scan job over a synthetic music folder."""
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from backend.models.database import AppDatabase
from backend.services.scan import run_scan


def _touch(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture
def library(tmp_path):
    db = AppDatabase(str(tmp_path / "libsync.db"))
    # Two library albums — one will auto-match, one will be ambiguous.
    db.upsert_album(source="qobuz", source_album_id="1",
                    title="Abbey Road", artist="The Beatles",
                    track_count=17, bit_depth=24, sample_rate=96.0)
    db.upsert_album(source="qobuz", source_album_id="2",
                    title="Revolver", artist="The Beatles",
                    track_count=14, bit_depth=24, sample_rate=96.0)
    return db


def _patch_mutagen(monkeypatch, specs: dict):
    """Patch mutagen.File to return fake tags keyed by first filename."""
    import backend.services.scan as scan_mod

    def fake_file(path, easy=True):
        for name, tags in specs.items():
            if name in path:
                fake_tags = MagicMock()
                fake_tags.get.side_effect = lambda k, default=None, t=tags: (
                    [t[k]] if k in t else default
                )
                info = MagicMock(
                    bits_per_sample=tags.get("_bd", 24),
                    sample_rate=tags.get("_sr_hz", 96000),
                )
                return MagicMock(tags=fake_tags, info=info)
        return None

    monkeypatch.setattr(scan_mod.mutagen, "File", fake_file)


@pytest.mark.asyncio
async def test_run_scan_classifies_folders(tmp_path, library, monkeypatch):
    music = tmp_path / "music"
    _touch(music / "The Beatles - Abbey Road" / "01.flac")
    _touch(music / "The Beatles - Revolver" / "01.flac")
    _touch(music / "Unknown Band - Whatever" / "01.flac")

    _patch_mutagen(monkeypatch, {
        "Abbey Road": {"albumartist": "The Beatles", "album": "Abbey Road", "_bd": 24},
        "Revolver": {"albumartist": "The Beatles", "album": "Revolver", "_bd": 16},
        "Whatever": {"albumartist": "Unknown Band", "album": "Whatever"},
    })

    event_bus = AsyncMock()

    result = await run_scan(
        library,
        download_path=str(music),
        dedup_db_dir=str(tmp_path),
        event_bus=event_bus,
        sentinel_write_enabled=False,
    )

    titles_auto = {e["album_id"] for e in result["auto_matched"]}
    assert 1 in titles_auto  # Abbey Road 24-bit both sides

    # Revolver: local 16-bit, library 24-bit → review
    review_ids = {c["album_id"]
                  for r in result["review"] for c in r["candidates"]}
    assert 2 in review_ids

    # Whatever: no library match
    assert any("Whatever" in u for u in result["unmatched"])

    # Progress events were emitted.
    event_bus.publish.assert_any_call("scan_progress",
                                      {"scanned": 3, "total": 3})


@pytest.mark.asyncio
async def test_run_scan_skips_sentineled_folders(tmp_path, library, monkeypatch):
    music = tmp_path / "music"
    album_dir = music / "The Beatles - Abbey Road"
    _touch(album_dir / "01.flac")
    _touch(album_dir / ".streamrip.json", '{"source":"qobuz","album_id":"1"}')

    _patch_mutagen(monkeypatch, {})
    event_bus = AsyncMock()

    result = await run_scan(
        library,
        download_path=str(music),
        dedup_db_dir=str(tmp_path),
        event_bus=event_bus,
        sentinel_write_enabled=False,
    )

    # The folder was counted as already-sentineled, not re-classified.
    assert result["sentinel_skipped"] == 1
    assert result["auto_matched"] == []
    assert result["review"] == []


@pytest.mark.asyncio
async def test_run_scan_walks_artist_album_layout(tmp_path, library, monkeypatch):
    music = tmp_path / "music"
    # Library seeds "Abbey Road" + "Revolver" by The Beatles in the library fixture.
    _touch(music / "The Beatles" / "(1969) Abbey Road [FLAC-24-96]" / "01.flac")
    _touch(music / "The Beatles" / "(1966) Revolver [FLAC-24-96]" / "01.flac")
    _touch(music / "_tuning" / "TestSignal.wav")  # no artist folder above

    _patch_mutagen(monkeypatch, {
        "Abbey Road": {"albumartist": "The Beatles", "album": "Abbey Road", "_bd": 24},
        "Revolver":   {"albumartist": "The Beatles", "album": "Revolver",   "_bd": 24},
        "TestSignal": {"albumartist": "Test", "album": "Tuning"},
    })
    event_bus = AsyncMock()

    result = await run_scan(
        library, download_path=str(music), dedup_db_dir=str(tmp_path),
        event_bus=event_bus, sentinel_write_enabled=False,
    )

    # Two library matches land in auto_matched; _tuning is unmatched.
    auto_ids = {e["album_id"] for e in result["auto_matched"]}
    assert auto_ids == {1, 2}
    assert any("_tuning" in u for u in result["unmatched"])
    assert result["scanned"] == 3


def test_find_album_folders_picks_leaves_with_audio(tmp_path):
    from backend.services.scan import _find_album_folders

    # Artist/Album/track.flac
    (tmp_path / "Artist A" / "Album 1").mkdir(parents=True)
    (tmp_path / "Artist A" / "Album 1" / "01.flac").touch()
    (tmp_path / "Artist A" / "Album 2").mkdir()
    (tmp_path / "Artist A" / "Album 2" / "01.mp3").touch()
    # Loose audio at top level
    (tmp_path / "_loose").mkdir()
    (tmp_path / "_loose" / "tone.wav").touch()
    # Empty artist dir (no audio anywhere below)
    (tmp_path / "Empty Artist" / "Empty Album").mkdir(parents=True)
    # Multi-disc album: Abbey Road with Disc 1/, Disc 2/ both containing audio
    # — we treat the disc leaves as two album candidates (acceptable compromise;
    # the matcher will see the same folder-name twice but a multi-disc album
    # really is one album; document in a comment that multi-disc libraries may
    # produce duplicate candidates).
    (tmp_path / "Beatles" / "Abbey Road" / "Disc 1").mkdir(parents=True)
    (tmp_path / "Beatles" / "Abbey Road" / "Disc 1" / "01.flac").touch()
    (tmp_path / "Beatles" / "Abbey Road" / "Disc 2").mkdir()
    (tmp_path / "Beatles" / "Abbey Road" / "Disc 2" / "01.flac").touch()

    found = _find_album_folders(tmp_path)
    names = {p.name for p in found}
    assert names == {"Album 1", "Album 2", "_loose", "Disc 1", "Disc 2"}
