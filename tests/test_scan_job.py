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
