"""Tag/folder-name metadata extraction."""
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.services.scan import FolderMeta, read_folder_metadata


def _make_folder(tmp_path: Path, name: str, files: list[str]) -> Path:
    folder = tmp_path / name
    folder.mkdir()
    for f in files:
        (folder / f).touch()
    return folder


def test_reads_tags_when_present(tmp_path):
    folder = _make_folder(tmp_path, "AlbumDir", ["01.flac", "02.flac", "cover.jpg"])
    fake_tags = MagicMock()
    fake_tags.get.side_effect = lambda key, default=None: {
        "albumartist": ["Radiohead"],
        "album": ["In Rainbows"],
    }.get(key, default)
    fake_info = MagicMock(bits_per_sample=24, sample_rate=96000)
    fake_file = MagicMock(tags=fake_tags, info=fake_info)

    with patch("backend.services.scan.mutagen.File", return_value=fake_file):
        meta = read_folder_metadata(folder)

    assert meta == FolderMeta(
        folder=folder,
        artist="Radiohead",
        album="In Rainbows",
        bit_depth=24,
        sample_rate=96.0,
        track_count=2,
        source="tags",
    )


def test_falls_back_to_folder_name(tmp_path):
    folder = _make_folder(tmp_path, "The Beatles - Abbey Road", ["a.flac"])

    with patch("backend.services.scan.mutagen.File", return_value=None):
        meta = read_folder_metadata(folder)

    assert meta is not None
    assert meta.artist == "The Beatles"
    assert meta.album == "Abbey Road"
    assert meta.bit_depth is None
    assert meta.source == "folder_name"


def test_returns_none_when_no_audio(tmp_path):
    folder = _make_folder(tmp_path, "Empty", ["readme.txt"])
    assert read_folder_metadata(folder) is None


def test_lossy_file_has_no_bit_depth(tmp_path):
    folder = _make_folder(tmp_path, "AlbumDir", ["01.mp3"])
    fake_tags = MagicMock()
    fake_tags.get.side_effect = lambda key, default=None: {
        "albumartist": ["Daft Punk"],
        "album": ["Random Access Memories"],
    }.get(key, default)
    fake_info = MagicMock(sample_rate=44100)
    # MP3 info has no bits_per_sample; simulate AttributeError
    del fake_info.bits_per_sample
    fake_file = MagicMock(tags=fake_tags, info=fake_info)

    with patch("backend.services.scan.mutagen.File", return_value=fake_file):
        meta = read_folder_metadata(folder)

    assert meta.bit_depth is None
    assert meta.sample_rate == 44.1
