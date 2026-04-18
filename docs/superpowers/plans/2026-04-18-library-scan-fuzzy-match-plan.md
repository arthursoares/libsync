# Library Scan — Fuzzy Matching + Manual Mark-as-Downloaded — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing sentinel-only "Scan Folder" to fuzzy-match pre-existing local album folders against the synced Qobuz/Tidal library, and add a manual "Mark as downloaded" affordance on album pages. Both paths share one primitive that flips `download_status`, stamps `downloaded_at`, stores `local_folder_path`, populates the per-source dedup DB, and best-effort writes a `.streamrip.json` sentinel.

**Architecture:** One new `backend/services/scan.py` module holds the pure-function normalizer + matcher + tag reader plus the mark/unmark primitives and the async scan job. New endpoints live in the existing `backend/api/library.py`. A schema v2 migration on `albums` adds `bit_depth`, `sample_rate`, `local_folder_path` — the first two populated during normal sync, the last set by the scan/mark flow. Frontend: a new `ScanReview.svelte` slide-over replaces the one-line text response; `AlbumDetail.svelte` gains a mark/unmark button.

**Tech Stack:** Python 3.10+, FastAPI, SQLite (`sqlite3` stdlib), `mutagen` (transitively available via the SDKs), SvelteKit (Svelte 5 runes), existing event-bus WebSocket.

**Reference spec:** `docs/superpowers/specs/2026-04-18-library-scan-fuzzy-match-design.md`

---

## File Structure

**Backend — create:**
- `backend/services/scan.py` — normalizer, matcher, tag reader, mark/unmark primitives, scan job (`ScanJob` dataclass + `run_scan` async function).
- `tests/test_scan_normalize.py` — normalizer unit tests.
- `tests/test_scan_matcher.py` — classification unit tests.
- `tests/test_scan_mark.py` — mark/unmark primitive unit tests.
- `tests/test_api_library_scan.py` — scan and mark API endpoint tests.

**Backend — modify:**
- `backend/models/database.py` — bump `SCHEMA_VERSION` to 2, add migration, extend `upsert_album` signature, add `get_all_albums_for_index`, `update_album_quality_meta`, `set_album_download_state`, `clear_album_download_state` helpers.
- `backend/services/library.py` — pass `bit_depth` / `sample_rate` into `upsert_album` from both extractors.
- `backend/api/library.py` — three new routes (`POST /scan-fuzzy`, `GET /scan-fuzzy/{job_id}`, `POST /albums/{id}/mark-downloaded`, `POST /albums/{id}/unmark-downloaded`).
- `backend/main.py` — register a module-level scan-job registry on `app.state` during lifespan setup.
- `backend/models/schemas.py` — `ConfigResponse` / `ConfigUpdate` gain `scan_sentinel_write_enabled: bool`.
- `backend/api/config.py` — allowlist the new key.

**Frontend — create:**
- `frontend/src/lib/components/ScanReview.svelte` — the three-section review slide-over.

**Frontend — modify:**
- `frontend/src/routes/settings/+page.svelte` — scan button opens `ScanReview`; subscribe to `scan_progress` / `scan_complete` events; add sentinel-write toggle.
- `frontend/src/lib/components/AlbumDetail.svelte` — mark / unmark button wired to the new endpoints.
- `frontend/src/lib/api.ts` (or wherever the fetch helpers live — to confirm during Task 12) — add `library.markDownloaded`, `library.unmarkDownloaded`, `library.scanFuzzy`, `library.scanFuzzyStatus`.

---

## Task 1: DB schema v2 migration

**Goal:** Add `bit_depth INTEGER`, `sample_rate REAL`, `local_folder_path TEXT` to `albums`. Backfill `bit_depth` / `sample_rate` from existing `quality` strings on migration.

**Files:**
- Modify: `backend/models/database.py`
- Test: `tests/test_database_migration.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_database_migration.py`:

```python
"""Schema v1 → v2 migration for the albums table."""
import sqlite3

from backend.models.database import AppDatabase, SCHEMA_VERSION


def test_v2_adds_expected_columns(tmp_path):
    db_path = str(tmp_path / "libsync.db")
    # Manually create a v1-shaped DB so we exercise the migration path.
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE albums (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            source_album_id TEXT NOT NULL,
            title TEXT NOT NULL,
            artist TEXT NOT NULL,
            quality TEXT,
            download_status TEXT NOT NULL DEFAULT 'not_downloaded',
            user_id INTEGER NOT NULL DEFAULT 1,
            UNIQUE(source, source_album_id, user_id)
        );
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (1);
        INSERT INTO albums (source, source_album_id, title, artist, quality)
            VALUES ('qobuz', '42', 'Abbey Road', 'The Beatles', 'FLAC 24/96kHz');
        INSERT INTO albums (source, source_album_id, title, artist, quality)
            VALUES ('qobuz', '43', 'Revolver', 'The Beatles', 'FLAC 16/44.1kHz');
        INSERT INTO albums (source, source_album_id, title, artist, quality)
            VALUES ('qobuz', '44', 'Unknown', 'Nobody', NULL);
        """
    )
    conn.commit()
    conn.close()

    AppDatabase(db_path)  # runs migration

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(albums)").fetchall()}
    assert {"bit_depth", "sample_rate", "local_folder_path"} <= cols

    version = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
    assert version == SCHEMA_VERSION == 2

    rows = {r["source_album_id"]: dict(r) for r in conn.execute(
        "SELECT source_album_id, bit_depth, sample_rate FROM albums"
    ).fetchall()}
    assert rows["42"]["bit_depth"] == 24 and rows["42"]["sample_rate"] == 96.0
    assert rows["43"]["bit_depth"] == 16 and rows["43"]["sample_rate"] == 44.1
    assert rows["44"]["bit_depth"] is None and rows["44"]["sample_rate"] is None


def test_v2_idempotent(tmp_path):
    db_path = str(tmp_path / "libsync.db")
    AppDatabase(db_path)
    AppDatabase(db_path)  # second open must be a no-op
    conn = sqlite3.connect(db_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(albums)").fetchall()}
    assert "bit_depth" in cols
    version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version == 2
```

- [ ] **Step 2: Run the test — expect failure**

Run: `poetry run pytest tests/test_database_migration.py -v`
Expected: FAIL — column `bit_depth` not found.

- [ ] **Step 3: Add the migration to `backend/models/database.py`**

First, update the top-level constant and schema (file top, around line 11 and 35):

```python
SCHEMA_VERSION = 2
```

Add the three new columns to the `CREATE TABLE IF NOT EXISTS albums` block in `SCHEMA_SQL` so fresh installs get them natively. Insert right after the existing `quality TEXT,` line:

```python
    quality TEXT,
    bit_depth INTEGER,
    sample_rate REAL,
    local_folder_path TEXT,
    file_size_bytes INTEGER,
```

(Keep the ordering: `quality` → new columns → existing `file_size_bytes`. Column position in SQLite is cosmetic, but keep the diff small.)

Replace `_init_schema` with:

```python
    def _init_schema(self):
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)
            row = conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)",
                    (SCHEMA_VERSION,),
                )
                return

            current = row["version"]
            if current < 2:
                self._migrate_to_v2(conn)
            conn.execute(
                "UPDATE schema_version SET version = ?",
                (SCHEMA_VERSION,),
            )

    def _migrate_to_v2(self, conn):
        """Schema v1 → v2: add bit_depth, sample_rate, local_folder_path.

        Backfills bit_depth / sample_rate best-effort by parsing the
        existing quality string (e.g. "FLAC 24/96kHz"). Rows whose
        quality doesn't match the pattern keep NULLs — the matcher
        treats NULL bit_depth as "unknown" (allows matching).
        """
        existing = {r[1] for r in conn.execute("PRAGMA table_info(albums)").fetchall()}
        if "bit_depth" not in existing:
            conn.execute("ALTER TABLE albums ADD COLUMN bit_depth INTEGER")
        if "sample_rate" not in existing:
            conn.execute("ALTER TABLE albums ADD COLUMN sample_rate REAL")
        if "local_folder_path" not in existing:
            conn.execute("ALTER TABLE albums ADD COLUMN local_folder_path TEXT")

        import re
        pattern = re.compile(r"(\d+)\s*/\s*([\d.]+)\s*kHz", re.IGNORECASE)
        rows = conn.execute(
            "SELECT id, quality FROM albums WHERE quality IS NOT NULL"
        ).fetchall()
        for row in rows:
            m = pattern.search(row["quality"] or "")
            if not m:
                continue
            try:
                bd = int(m.group(1))
                sr = float(m.group(2))
            except ValueError:
                continue
            conn.execute(
                "UPDATE albums SET bit_depth = ?, sample_rate = ? WHERE id = ?",
                (bd, sr, row["id"]),
            )
```

- [ ] **Step 4: Run the test — expect pass**

Run: `poetry run pytest tests/test_database_migration.py -v`
Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/models/database.py tests/test_database_migration.py
git commit -m "feat(db): schema v2 with bit_depth/sample_rate/local_folder_path on albums"
```

---

## Task 2: Persist bit_depth / sample_rate during sync

**Goal:** `upsert_album` accepts the two fields; the Qobuz + Tidal extractors pass them through. Backfills on next sync.

**Files:**
- Modify: `backend/models/database.py` (`upsert_album` signature + SQL)
- Modify: `backend/services/library.py` (`_extract_qobuz_album`, `_extract_tidal_album`)
- Test: `tests/test_database_migration.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_database_migration.py`:

```python
def test_upsert_album_persists_quality_meta(tmp_path):
    db = AppDatabase(str(tmp_path / "libsync.db"))
    album_id = db.upsert_album(
        source="qobuz",
        source_album_id="100",
        title="Random",
        artist="Daft Punk",
        bit_depth=24,
        sample_rate=44.1,
    )
    row = db.get_album(album_id)
    assert row["bit_depth"] == 24
    assert row["sample_rate"] == 44.1


def test_upsert_album_bit_depth_backward_compatible(tmp_path):
    db = AppDatabase(str(tmp_path / "libsync.db"))
    album_id = db.upsert_album(
        source="qobuz", source_album_id="101", title="X", artist="Y"
    )
    row = db.get_album(album_id)
    assert row["bit_depth"] is None
    assert row["sample_rate"] is None
```

- [ ] **Step 2: Run the test — expect failure**

Run: `poetry run pytest tests/test_database_migration.py::test_upsert_album_persists_quality_meta -v`
Expected: FAIL — `upsert_album()` got an unexpected keyword argument `bit_depth`.

- [ ] **Step 3: Extend `upsert_album`**

In `backend/models/database.py`, extend the signature and SQL. Add kwargs before `added_to_library_at`:

```python
    def upsert_album(
        self,
        source: str,
        source_album_id: str,
        title: str,
        artist: str,
        release_date: str | None = None,
        label: str | None = None,
        genre: str | None = None,
        track_count: int | None = None,
        duration_seconds: int | None = None,
        cover_url: str | None = None,
        quality: str | None = None,
        bit_depth: int | None = None,
        sample_rate: float | None = None,
        added_to_library_at: str | None = None,
        user_id: int = 1,
    ) -> int:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO albums
                   (source, source_album_id, title, artist, release_date, label,
                    genre, track_count, duration_seconds, cover_url, quality,
                    bit_depth, sample_rate, added_to_library_at, user_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source, source_album_id, user_id)
                   DO UPDATE SET
                     title=excluded.title, artist=excluded.artist,
                     release_date=excluded.release_date, label=excluded.label,
                     genre=excluded.genre, track_count=excluded.track_count,
                     duration_seconds=excluded.duration_seconds,
                     cover_url=excluded.cover_url, quality=excluded.quality,
                     bit_depth=COALESCE(excluded.bit_depth, albums.bit_depth),
                     sample_rate=COALESCE(excluded.sample_rate, albums.sample_rate),
                     added_to_library_at=COALESCE(
                         excluded.added_to_library_at,
                         albums.added_to_library_at
                     )
                """,
                (
                    source, source_album_id, title, artist, release_date,
                    label, genre, track_count, duration_seconds, cover_url,
                    quality, bit_depth, sample_rate,
                    added_to_library_at, user_id,
                ),
            )
            row = conn.execute(
                "SELECT id FROM albums WHERE source=? AND source_album_id=? AND user_id=?",
                (source, source_album_id, user_id),
            ).fetchone()
            return row["id"]
```

- [ ] **Step 4: Update the library extractors**

In `backend/services/library.py`, amend `_extract_qobuz_album`:

```python
    def _extract_qobuz_album(self, item):
        album = item.get("album", item) if "album" in item else item
        artist = album.get("artist", {})
        image = album.get("image", {})
        bit_depth = album.get("maximum_bit_depth", 16)
        sample_rate = album.get("maximum_sampling_rate", 44.1)
        quality = f"FLAC {bit_depth}/{sample_rate}kHz" if bit_depth else None
        return {
            "source": "qobuz", "source_album_id": str(album["id"]),
            "title": album.get("title", "Unknown"),
            "artist": artist.get("name", "Unknown") if isinstance(artist, dict) else str(artist),
            "release_date": album.get("release_date_original"),
            "label": album.get("label", {}).get("name") if isinstance(album.get("label"), dict) else None,
            "genre": album.get("genre", {}).get("name") if isinstance(album.get("genre"), dict) else None,
            "track_count": album.get("tracks_count"),
            "duration_seconds": album.get("duration"),
            "cover_url": image.get("large") or image.get("small"),
            "quality": quality,
            "bit_depth": int(bit_depth) if bit_depth else None,
            "sample_rate": float(sample_rate) if sample_rate else None,
        }
```

Amend `_extract_tidal_album` — Tidal reports quality as a string enum (`"HI_RES_LOSSLESS"`, `"LOSSLESS"`, `"HIGH"`, `"LOW"`). Map to bit_depth/sample_rate best-effort:

```python
    _TIDAL_QUALITY_META = {
        "HI_RES_LOSSLESS": (24, 192.0),
        "HI_RES": (24, 96.0),
        "LOSSLESS": (16, 44.1),
        "HIGH": (None, None),
        "LOW": (None, None),
    }

    def _extract_tidal_album(self, item):
        album = item.get("item", item) if "item" in item else item
        artists = album.get("artists", [])
        artist_name = ", ".join(a["name"] for a in artists) if artists else album.get("artist", {}).get("name", "Unknown")
        cover = album.get("cover", "")
        cover_url = f"https://resources.tidal.com/images/{cover.replace('-', '/')}/640x640.jpg" if cover else None
        aq = album.get("audioQuality")
        bit_depth, sample_rate = self._TIDAL_QUALITY_META.get(aq, (None, None))
        return {
            "source": "tidal", "source_album_id": str(album["id"]),
            "title": album.get("title", "Unknown"), "artist": artist_name,
            "release_date": album.get("releaseDate"),
            "track_count": album.get("numberOfTracks"),
            "duration_seconds": album.get("duration"),
            "cover_url": cover_url, "quality": aq,
            "bit_depth": bit_depth,
            "sample_rate": sample_rate,
        }
```

- [ ] **Step 5: Run the tests — expect pass**

Run: `poetry run pytest tests/test_database_migration.py -v`
Expected: PASS.

Run: `poetry run pytest tests/ -q -k "not test_e2e"`
Expected: all existing tests still pass (the extractor change adds keys, doesn't remove).

- [ ] **Step 6: Commit**

```bash
git add backend/models/database.py backend/services/library.py tests/test_database_migration.py
git commit -m "feat(sync): persist bit_depth and sample_rate from Qobuz/Tidal metadata"
```

---

## Task 3: Normalizer

**Goal:** Pure function `normalize(s: str) -> str` for artist/album string comparison. Covers casefold, `"The "` prefix stripping, NFKD diacritic stripping, parenthetical/bracket suffix stripping, whitespace collapse.

**Files:**
- Create: `backend/services/scan.py`
- Test: `tests/test_scan_normalize.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_scan_normalize.py`:

```python
"""Normalizer for fuzzy artist/album matching."""
import pytest

from backend.services.scan import normalize


@pytest.mark.parametrize("raw,expected", [
    ("The Beatles", "beatles"),
    ("The The", "the"),  # only leading "The " stripped once
    ("Beyoncé", "beyonce"),
    ("Café del Mar", "cafe del mar"),
    ("Abbey Road (Remastered 2019)", "abbey road"),
    ("Abbey Road [Deluxe Edition]", "abbey road"),
    ("Abbey Road  (Deluxe)  (Remastered)", "abbey road"),
    ("  Extra   Spaces  ", "extra spaces"),
    ("AC/DC", "ac/dc"),
    ("Sigur Rós", "sigur ros"),
    ("", ""),
])
def test_normalize_cases(raw, expected):
    assert normalize(raw) == expected


def test_normalize_handles_none():
    assert normalize(None) == ""
```

- [ ] **Step 2: Run the test — expect failure**

Run: `poetry run pytest tests/test_scan_normalize.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.services.scan'`.

- [ ] **Step 3: Implement the normalizer**

Create `backend/services/scan.py`:

```python
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
```

- [ ] **Step 4: Run the test — expect pass**

Run: `poetry run pytest tests/test_scan_normalize.py -v`
Expected: all parametrized cases PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/services/scan.py tests/test_scan_normalize.py
git commit -m "feat(scan): normalizer for fuzzy artist/album matching"
```

---

## Task 4: Tag reader

**Goal:** `read_folder_metadata(folder: Path) -> FolderMeta | None` — walks a folder, reads tags from the first audio file via `mutagen`, falls back to folder-name parsing. Deterministic (sorted file list). Returns `None` if no audio files found.

**Files:**
- Modify: `backend/services/scan.py`
- Test: `tests/test_scan_tag_reader.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_scan_tag_reader.py`:

```python
"""Tag/folder-name metadata extraction."""
from pathlib import Path
from unittest.mock import patch, MagicMock

from backend.services.scan import read_folder_metadata, FolderMeta


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
```

- [ ] **Step 2: Run the test — expect failure**

Run: `poetry run pytest tests/test_scan_tag_reader.py -v`
Expected: FAIL — `ImportError: cannot import name 'read_folder_metadata'`.

- [ ] **Step 3: Implement the tag reader**

Append to `backend/services/scan.py`:

```python
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
```

- [ ] **Step 4: Run the test — expect pass**

Run: `poetry run pytest tests/test_scan_tag_reader.py -v`
Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/services/scan.py tests/test_scan_tag_reader.py
git commit -m "feat(scan): audio-tag + folder-name metadata reader"
```

---

## Task 5: Matcher / classifier

**Goal:** `classify(folder_meta, library_index) -> MatchResult`. Given a `FolderMeta` and a pre-built index of library albums (keyed by normalized `(artist, title)`), return a classification: `auto_match`, `review`, or `unmatched`.

**Files:**
- Modify: `backend/services/scan.py`
- Test: `tests/test_scan_matcher.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_scan_matcher.py`:

```python
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
```

- [ ] **Step 2: Run the test — expect failure**

Run: `poetry run pytest tests/test_scan_matcher.py -v`
Expected: FAIL — `ImportError: cannot import name 'classify'`.

- [ ] **Step 3: Implement the matcher**

Append to `backend/services/scan.py`:

```python
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
```

- [ ] **Step 4: Run the test — expect pass**

Run: `poetry run pytest tests/test_scan_matcher.py -v`
Expected: all 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/services/scan.py tests/test_scan_matcher.py
git commit -m "feat(scan): classifier distinguishing auto-match / review / unmatched"
```

---

## Task 6: DB helpers for mark/unmark

**Goal:** Three focused helpers on `AppDatabase`: `get_all_albums_for_index`, `set_album_download_state`, `clear_album_download_state`. These are DB-only; filesystem + dedup-DB side-effects live in `scan.py`.

**Files:**
- Modify: `backend/models/database.py`
- Test: `tests/test_database_migration.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_database_migration.py`:

```python
def test_get_all_albums_for_index(tmp_path):
    db = AppDatabase(str(tmp_path / "libsync.db"))
    db.upsert_album(source="qobuz", source_album_id="1",
                    title="Abbey Road", artist="The Beatles",
                    bit_depth=24, sample_rate=96.0)
    db.upsert_album(source="tidal", source_album_id="2",
                    title="In Rainbows", artist="Radiohead")

    rows = db.get_all_albums_for_index()
    by_title = {r["title"]: r for r in rows}
    assert set(by_title) == {"Abbey Road", "In Rainbows"}
    assert by_title["Abbey Road"]["bit_depth"] == 24
    assert {"id", "source", "artist", "title", "bit_depth", "sample_rate"} <= set(rows[0])


def test_set_and_clear_album_download_state(tmp_path):
    db = AppDatabase(str(tmp_path / "libsync.db"))
    album_id = db.upsert_album(source="qobuz", source_album_id="1",
                               title="X", artist="Y")

    db.set_album_download_state(album_id, downloaded_at="2026-04-18T10:00:00",
                                local_folder_path="/music/X")
    row = db.get_album(album_id)
    assert row["download_status"] == "complete"
    assert row["downloaded_at"] == "2026-04-18T10:00:00"
    assert row["local_folder_path"] == "/music/X"

    db.clear_album_download_state(album_id)
    row = db.get_album(album_id)
    assert row["download_status"] == "not_downloaded"
    assert row["downloaded_at"] is None
    assert row["local_folder_path"] is None
```

- [ ] **Step 2: Run the test — expect failure**

Run: `poetry run pytest tests/test_database_migration.py -v`
Expected: FAIL — missing methods.

- [ ] **Step 3: Implement the helpers**

In `backend/models/database.py`, add these methods after `update_album_status`:

```python
    def get_all_albums_for_index(self, user_id: int = 1) -> list[dict]:
        """Return every album as a lean dict for building a match index."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT id, source, source_album_id, artist, title,
                          bit_depth, sample_rate, track_count, download_status
                   FROM albums WHERE user_id = ?""",
                (user_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def set_album_download_state(
        self,
        album_id: int,
        *,
        downloaded_at: str,
        local_folder_path: str | None = None,
    ) -> None:
        """Mark album complete, optionally recording the local folder path."""
        with self._connect() as conn:
            conn.execute(
                """UPDATE albums
                   SET download_status = 'complete',
                       downloaded_at = ?,
                       local_folder_path = COALESCE(?, local_folder_path)
                   WHERE id = ?""",
                (downloaded_at, local_folder_path, album_id),
            )

    def clear_album_download_state(self, album_id: int) -> None:
        """Reverse set_album_download_state — back to not_downloaded."""
        with self._connect() as conn:
            conn.execute(
                """UPDATE albums
                   SET download_status = 'not_downloaded',
                       downloaded_at = NULL,
                       local_folder_path = NULL
                   WHERE id = ?""",
                (album_id,),
            )
```

- [ ] **Step 4: Run the test — expect pass**

Run: `poetry run pytest tests/test_database_migration.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/models/database.py tests/test_database_migration.py
git commit -m "feat(db): helpers for building match index and setting/clearing download state"
```

---

## Task 7: mark_album_downloaded / unmark_album_downloaded

**Goal:** Side-effectful primitives in `scan.py`. Combine DB update, dedup-DB population, and optional sentinel write. Read-only filesystem errors degrade gracefully.

**Files:**
- Modify: `backend/services/scan.py`
- Test: `tests/test_scan_mark.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_scan_mark.py`:

```python
"""mark_album_downloaded / unmark_album_downloaded primitives."""
import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend.models.database import AppDatabase
from backend.services.scan import (
    mark_album_downloaded,
    unmark_album_downloaded,
)


@pytest.fixture
def db(tmp_path):
    d = AppDatabase(str(tmp_path / "libsync.db"))
    album_id = d.upsert_album(
        source="qobuz", source_album_id="42",
        title="Abbey Road", artist="The Beatles",
        track_count=17, bit_depth=24, sample_rate=96.0,
    )
    d.upsert_track(album_id=album_id, source_track_id="t1",
                   title="Come Together", artist="The Beatles",
                   track_number=1)
    d.upsert_track(album_id=album_id, source_track_id="t2",
                   title="Something", artist="The Beatles",
                   track_number=2)
    return d, album_id


def _dedup_path(tmp_path, source):
    fname = "downloads.db" if source == "qobuz" else f"downloads-{source}.db"
    return str(tmp_path / fname)


def _dedup_rows(path):
    if not os.path.exists(path):
        return []
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT * FROM downloads").fetchall()
        return rows
    finally:
        conn.close()


def test_mark_updates_db_and_writes_sentinel(tmp_path, db):
    d, album_id = db
    folder = tmp_path / "music" / "Beatles - Abbey Road"
    folder.mkdir(parents=True)

    mark_album_downloaded(
        d, album_id,
        local_folder_path=str(folder),
        dedup_db_dir=str(tmp_path),
        sentinel_write_enabled=True,
    )

    row = d.get_album(album_id)
    assert row["download_status"] == "complete"
    assert row["downloaded_at"] is not None
    assert row["local_folder_path"] == str(folder)

    sentinel = folder / ".streamrip.json"
    assert sentinel.exists()
    payload = json.loads(sentinel.read_text())
    assert payload["source"] == "qobuz"
    assert payload["album_id"] == "42"
    assert payload["title"] == "Abbey Road"
    assert payload["tracks_count"] == 17


def test_mark_populates_dedup_db(tmp_path, db):
    d, album_id = db
    folder = tmp_path / "music" / "X"
    folder.mkdir(parents=True)

    mark_album_downloaded(
        d, album_id,
        local_folder_path=str(folder),
        dedup_db_dir=str(tmp_path),
        sentinel_write_enabled=False,
    )

    rows = _dedup_rows(_dedup_path(tmp_path, "qobuz"))
    # Two tracks were added in the fixture.
    assert len(rows) == 2


def test_mark_without_folder_is_db_only(tmp_path, db):
    d, album_id = db
    mark_album_downloaded(
        d, album_id,
        local_folder_path=None,
        dedup_db_dir=str(tmp_path),
        sentinel_write_enabled=True,
    )
    row = d.get_album(album_id)
    assert row["download_status"] == "complete"
    assert row["local_folder_path"] is None


def test_mark_idempotent(tmp_path, db):
    d, album_id = db
    folder = tmp_path / "music" / "X"
    folder.mkdir(parents=True)

    mark_album_downloaded(d, album_id, local_folder_path=str(folder),
                          dedup_db_dir=str(tmp_path),
                          sentinel_write_enabled=True)
    mark_album_downloaded(d, album_id, local_folder_path=str(folder),
                          dedup_db_dir=str(tmp_path),
                          sentinel_write_enabled=True)

    rows = _dedup_rows(_dedup_path(tmp_path, "qobuz"))
    # No duplicate rows despite two calls.
    assert len(rows) == 2


def test_mark_graceful_on_readonly_folder(tmp_path, db, caplog):
    d, album_id = db
    folder = tmp_path / "music" / "X"
    folder.mkdir(parents=True)
    folder.chmod(0o555)  # read + execute, no write

    try:
        mark_album_downloaded(
            d, album_id, local_folder_path=str(folder),
            dedup_db_dir=str(tmp_path),
            sentinel_write_enabled=True,
        )
    finally:
        folder.chmod(0o755)

    # DB still updated despite sentinel write failure.
    row = d.get_album(album_id)
    assert row["download_status"] == "complete"
    assert not (folder / ".streamrip.json").exists()


def test_mark_skips_sentinel_when_disabled(tmp_path, db):
    d, album_id = db
    folder = tmp_path / "music" / "X"
    folder.mkdir(parents=True)

    mark_album_downloaded(
        d, album_id, local_folder_path=str(folder),
        dedup_db_dir=str(tmp_path),
        sentinel_write_enabled=False,
    )
    assert not (folder / ".streamrip.json").exists()
    assert d.get_album(album_id)["download_status"] == "complete"


def test_unmark_reverses_everything(tmp_path, db):
    d, album_id = db
    folder = tmp_path / "music" / "X"
    folder.mkdir(parents=True)

    mark_album_downloaded(d, album_id, local_folder_path=str(folder),
                          dedup_db_dir=str(tmp_path),
                          sentinel_write_enabled=True)
    unmark_album_downloaded(d, album_id, dedup_db_dir=str(tmp_path))

    row = d.get_album(album_id)
    assert row["download_status"] == "not_downloaded"
    assert row["downloaded_at"] is None
    assert row["local_folder_path"] is None
    assert not (folder / ".streamrip.json").exists()
    assert _dedup_rows(_dedup_path(tmp_path, "qobuz")) == []
```

- [ ] **Step 2: Run the tests — expect failure**

Run: `poetry run pytest tests/test_scan_mark.py -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement the primitives**

Append to `backend/services/scan.py`:

```python
import json
import logging
import os
import sqlite3
from datetime import datetime

logger = logging.getLogger("streamrip")

_DEDUP_SCHEMA = """
CREATE TABLE IF NOT EXISTS downloads (
    id TEXT PRIMARY KEY
);
"""


def _dedup_db_path(source: str, dedup_db_dir: str) -> str:
    fname = "downloads.db" if source == "qobuz" else f"downloads-{source}.db"
    return os.path.join(dedup_db_dir, fname)


def _populate_dedup(track_ids: list[str], source: str, dedup_db_dir: str) -> None:
    """Insert track IDs into the per-source dedup DB. Idempotent."""
    path = _dedup_db_path(source, dedup_db_dir)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_DEDUP_SCHEMA)
        conn.executemany(
            "INSERT OR IGNORE INTO downloads (id) VALUES (?)",
            [(tid,) for tid in track_ids],
        )
        conn.commit()
    finally:
        conn.close()


def _remove_from_dedup(track_ids: list[str], source: str, dedup_db_dir: str) -> None:
    path = _dedup_db_path(source, dedup_db_dir)
    if not os.path.exists(path):
        return
    conn = sqlite3.connect(path)
    try:
        conn.executemany(
            "DELETE FROM downloads WHERE id = ?",
            [(tid,) for tid in track_ids],
        )
        conn.commit()
    finally:
        conn.close()


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
    sentinel_write_enabled: bool = True,
    now: datetime | None = None,
) -> None:
    """Mark an album complete in DB, dedup DB, and optionally on disk.

    Idempotent — calling repeatedly is safe. Sentinel writes degrade
    gracefully on read-only mounts.
    """
    album = db.get_album(album_id)
    if album is None:
        raise ValueError(f"Album {album_id} not found")

    downloaded_at = (now or datetime.now()).isoformat()
    db.set_album_download_state(
        album_id,
        downloaded_at=downloaded_at,
        local_folder_path=local_folder_path,
    )

    track_ids = [t["source_track_id"] for t in db.get_tracks(album_id)]
    if track_ids:
        _populate_dedup(track_ids, album["source"], dedup_db_dir)

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
) -> None:
    album = db.get_album(album_id)
    if album is None:
        raise ValueError(f"Album {album_id} not found")

    folder = album.get("local_folder_path")
    _remove_sentinel(folder)

    track_ids = [t["source_track_id"] for t in db.get_tracks(album_id)]
    if track_ids:
        _remove_from_dedup(track_ids, album["source"], dedup_db_dir)

    db.clear_album_download_state(album_id)
```

- [ ] **Step 4: Run the tests — expect pass**

Run: `poetry run pytest tests/test_scan_mark.py -v`
Expected: all 7 tests PASS.

Note: the read-only test uses `chmod(0o555)`. If running as root (common in Docker), that test will skip-or-fail oddly — an acceptable limitation. Add `pytest.skip` guard if CI flags it.

- [ ] **Step 5: Commit**

```bash
git add backend/services/scan.py tests/test_scan_mark.py
git commit -m "feat(scan): mark/unmark album primitives with sentinel + dedup side effects"
```

---

## Task 8: Scan job

**Goal:** `run_scan(db, download_path, dedup_db_dir, event_bus, sentinel_write_enabled)` async function: walks `download_path`, classifies each child folder, auto-marks exact matches, collects review/unmatched, emits progress events, returns full result.

**Files:**
- Modify: `backend/services/scan.py`
- Test: `tests/test_scan_job.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_scan_job.py`:

```python
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
```

Ensure `pytest-asyncio` is declared (it's likely already used; check `pyproject.toml`). If missing, add to plan notes.

- [ ] **Step 2: Run the tests — expect failure**

Run: `poetry run pytest tests/test_scan_job.py -v`
Expected: FAIL — `ImportError: cannot import name 'run_scan'`.

- [ ] **Step 3: Implement `run_scan`**

Append to `backend/services/scan.py`:

```python
import asyncio


async def run_scan(
    db,
    *,
    download_path: str,
    dedup_db_dir: str,
    event_bus,
    sentinel_write_enabled: bool = True,
) -> dict:
    """Walk the download path, classify each album folder, auto-mark
    exact matches, and return a review/unmatched report.

    Emits `scan_progress` after each folder and `scan_complete` at the
    end via the provided event bus. File-system work runs in a worker
    thread so the event loop stays responsive.
    """
    index = build_library_index(db.get_all_albums_for_index())

    root = Path(download_path)
    if not root.is_dir():
        return {
            "status": "complete",
            "scanned": 0,
            "sentinel_skipped": 0,
            "auto_matched": [],
            "review": [],
            "unmatched": [],
        }

    folders = [p for p in sorted(root.iterdir()) if p.is_dir()]
    total = len(folders)

    auto_matched: list[dict] = []
    review: list[dict] = []
    unmatched: list[str] = []
    sentinel_skipped = 0

    for i, folder in enumerate(folders, start=1):
        # The existing sentinel-scan endpoint already reconciles these;
        # skip so we don't double-count.
        if (folder / ".streamrip.json").exists():
            sentinel_skipped += 1
            await event_bus.publish("scan_progress", {"scanned": i, "total": total})
            continue

        meta = await asyncio.to_thread(read_folder_metadata, folder)
        if meta is None:
            await event_bus.publish("scan_progress", {"scanned": i, "total": total})
            continue

        result = classify(meta, index)

        if result.kind == "auto_match":
            await asyncio.to_thread(
                mark_album_downloaded,
                db, result.album_id,
                local_folder_path=str(folder),
                dedup_db_dir=dedup_db_dir,
                sentinel_write_enabled=sentinel_write_enabled,
            )
            auto_matched.append({
                "album_id": result.album_id,
                "folder": str(folder),
                "reason": result.reason,
            })
        elif result.kind == "review":
            review.append({
                "folder": str(folder),
                "local_bit_depth": meta.bit_depth,
                "local_sample_rate": meta.sample_rate,
                "candidates": [
                    {
                        "album_id": c.album_id, "source": c.source,
                        "artist": c.artist, "title": c.title,
                        "score": c.score, "reason": c.reason,
                    }
                    for c in result.candidates
                ],
            })
        else:  # unmatched
            unmatched.append(str(folder))

        await event_bus.publish("scan_progress", {"scanned": i, "total": total})

    payload = {
        "status": "complete",
        "scanned": total,
        "sentinel_skipped": sentinel_skipped,
        "auto_matched": auto_matched,
        "review": review,
        "unmatched": unmatched,
    }
    await event_bus.publish("scan_complete", payload)
    return payload
```

- [ ] **Step 4: Run the tests — expect pass**

Run: `poetry run pytest tests/test_scan_job.py -v`
Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/services/scan.py tests/test_scan_job.py
git commit -m "feat(scan): async scan job that classifies folders and auto-marks exacts"
```

---

## Task 9: Config toggle `scan_sentinel_write_enabled`

**Goal:** Add a bool config key wired through the config schema/API so users can disable sentinel writes for read-only mounts.

**Files:**
- Modify: `backend/models/schemas.py`
- Modify: `backend/api/config.py`
- Modify: `frontend/src/routes/settings/+page.svelte` (new toggle)
- Test: extend `tests/test_api_routes.py` — just verify the field round-trips

- [ ] **Step 1: Write the failing test**

In `tests/test_api_routes.py`, find the config-related test block and add:

```python
def test_config_round_trip_includes_scan_sentinel_toggle(client):
    resp = client.patch("/api/config", json={"scan_sentinel_write_enabled": False})
    assert resp.status_code == 200
    got = client.get("/api/config").json()
    assert got["scan_sentinel_write_enabled"] is False
```

(Reuse the existing `client` fixture.)

- [ ] **Step 2: Run — expect failure**

Run: `poetry run pytest tests/test_api_routes.py::test_config_round_trip_includes_scan_sentinel_toggle -v`
Expected: FAIL (field unknown to schema).

- [ ] **Step 3: Add the field**

In `backend/models/schemas.py`, add to both `AppConfig` (class at line 114, the GET response) and `ConfigUpdate` (class at line 135):

```python
# In AppConfig:
    scan_sentinel_write_enabled: bool = True

# In ConfigUpdate:
    scan_sentinel_write_enabled: bool | None = None
```

In `backend/api/config.py`, the bool-coercion lives in the GET handler (lines 20-22). Extend the tuple:

```python
        elif key in ("auto_sync_enabled", "qobuz_download_booklets",
                      "source_subdirectories", "disc_subdirectories", "embed_artwork",
                      "scan_sentinel_write_enabled"):
            config_dict[key] = value.lower() in ("true", "1", "yes")
```

(No write-path change needed; `update_config` already stringifies generically at line 53.)

- [ ] **Step 4: Wire up frontend toggle**

In `frontend/src/routes/settings/+page.svelte`, add state:

```javascript
  let scanSentinelWriteEnabled = $state(true);
```

Load in the `onMount` effect alongside other config:

```javascript
        scanSentinelWriteEnabled = config.scan_sentinel_write_enabled ?? true;
```

Include in `saveSettings`:

```javascript
        scan_sentinel_write_enabled: scanSentinelWriteEnabled,
```

Add a settings-row near the existing Scan Folder button (the "Downloads" section). Show a toggle with label: "Write sentinel file in album folders — disable when scanning a read-only mount."

- [ ] **Step 5: Run the tests**

Run: `poetry run pytest tests/test_api_routes.py -v`
Expected: the new test PASSES; existing tests still PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/models/schemas.py backend/api/config.py \
        frontend/src/routes/settings/+page.svelte tests/test_api_routes.py
git commit -m "feat(scan): scan_sentinel_write_enabled config toggle for read-only mounts"
```

---

## Task 10: API endpoints for mark / unmark

**Goal:** `POST /api/library/albums/{id}/mark-downloaded` and `POST .../unmark-downloaded`. Thin wrappers around the primitives.

**Files:**
- Modify: `backend/api/library.py`
- Modify: `backend/models/schemas.py` (request body)
- Test: `tests/test_api_library_scan.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_library_scan.py`:

```python
"""API tests for /scan-fuzzy, /mark-downloaded, /unmark-downloaded."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client_with_album(client):
    # `client` fixture creates a fresh AppDatabase on app.state.db.
    db = client.app.state.db
    album_id = db.upsert_album(
        source="qobuz", source_album_id="42",
        title="Abbey Road", artist="The Beatles",
        track_count=17, bit_depth=24, sample_rate=96.0,
    )
    db.upsert_track(album_id=album_id, source_track_id="t1",
                    title="Come Together", artist="The Beatles",
                    track_number=1)
    return client, album_id


def test_mark_downloaded_happy_path(client_with_album):
    client, album_id = client_with_album
    resp = client.post(
        f"/api/library/albums/{album_id}/mark-downloaded",
        json={"local_folder_path": None},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["download_status"] == "complete"


def test_mark_downloaded_with_folder(client_with_album, tmp_path):
    client, album_id = client_with_album
    folder = tmp_path / "Beatles - Abbey Road"
    folder.mkdir()
    resp = client.post(
        f"/api/library/albums/{album_id}/mark-downloaded",
        json={"local_folder_path": str(folder)},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["local_folder_path"] == str(folder)


def test_unmark_downloaded(client_with_album):
    client, album_id = client_with_album
    client.post(f"/api/library/albums/{album_id}/mark-downloaded", json={})
    resp = client.post(f"/api/library/albums/{album_id}/unmark-downloaded")
    assert resp.status_code == 200
    assert resp.json()["download_status"] == "not_downloaded"


def test_mark_unknown_album_returns_404(client):
    resp = client.post("/api/library/albums/99999/mark-downloaded", json={})
    assert resp.status_code == 404
```

- [ ] **Step 2: Run — expect failure**

Run: `poetry run pytest tests/test_api_library_scan.py -v`
Expected: 404 for all (routes don't exist).

- [ ] **Step 3: Add request schema**

In `backend/models/schemas.py`, add:

```python
class MarkDownloadedRequest(BaseModel):
    local_folder_path: str | None = None
```

- [ ] **Step 4: Add routes**

In `backend/api/library.py`:

```python
import os
from fastapi.responses import JSONResponse

from ..models.schemas import MarkDownloadedRequest
from ..services.scan import mark_album_downloaded, unmark_album_downloaded


def _dedup_db_dir() -> str:
    db_path = os.environ.get("STREAMRIP_DB_PATH", "data/streamrip.db")
    return os.path.dirname(db_path) or "data"


@router.post("/albums/{album_id}/mark-downloaded")
async def mark_downloaded(
    request: Request, album_id: int, body: MarkDownloadedRequest
):
    db = request.app.state.db
    if db.get_album(album_id) is None:
        return JSONResponse({"error": "Album not found"}, status_code=404)

    sentinel_enabled = (
        (db.get_config("scan_sentinel_write_enabled") or "True") == "True"
    )
    mark_album_downloaded(
        db, album_id,
        local_folder_path=body.local_folder_path,
        dedup_db_dir=_dedup_db_dir(),
        sentinel_write_enabled=sentinel_enabled,
    )
    await request.app.state.event_bus.publish(
        "album_status_changed",
        {"album_id": album_id, "status": "complete"},
    )
    return db.get_album(album_id)


@router.post("/albums/{album_id}/unmark-downloaded")
async def unmark_downloaded(request: Request, album_id: int):
    db = request.app.state.db
    if db.get_album(album_id) is None:
        return JSONResponse({"error": "Album not found"}, status_code=404)

    unmark_album_downloaded(
        db, album_id, dedup_db_dir=_dedup_db_dir(),
    )
    await request.app.state.event_bus.publish(
        "album_status_changed",
        {"album_id": album_id, "status": "not_downloaded"},
    )
    return db.get_album(album_id)
```

- [ ] **Step 5: Run the tests — expect pass**

Run: `poetry run pytest tests/test_api_library_scan.py -v`
Expected: all 4 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/api/library.py backend/models/schemas.py tests/test_api_library_scan.py
git commit -m "feat(api): mark-downloaded / unmark-downloaded endpoints"
```

---

## Task 11: API endpoints for scan-fuzzy

**Goal:** `POST /api/library/scan-fuzzy` starts a background job, returns `{job_id}`. `GET /api/library/scan-fuzzy/{job_id}` returns status + result. One job at a time — concurrent starts return 409.

**Files:**
- Modify: `backend/api/library.py`
- Modify: `backend/main.py` (register a `scan_jobs` registry on `app.state`)
- Test: `tests/test_api_library_scan.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api_library_scan.py`:

```python
def test_scan_fuzzy_starts_job_and_returns_id(client_with_album, tmp_path):
    client, _ = client_with_album
    client.app.state.db.set_config("downloads_path", str(tmp_path / "music"))
    (tmp_path / "music").mkdir()

    resp = client.post("/api/library/scan-fuzzy")
    assert resp.status_code == 200
    body = resp.json()
    assert "job_id" in body


def test_scan_fuzzy_concurrent_returns_409(client_with_album, tmp_path, monkeypatch):
    client, _ = client_with_album
    client.app.state.db.set_config("downloads_path", str(tmp_path / "music"))
    (tmp_path / "music").mkdir()

    # Force the job to hang so we can fire a concurrent start. The route
    # imports the `scan` module and looks up `run_scan` at call time, so
    # monkeypatching the attribute on the module is what we want.
    import backend.services.scan as scan_mod

    original = scan_mod.run_scan

    async def slow(*args, **kwargs):
        import asyncio
        await asyncio.sleep(0.3)
        return await original(*args, **kwargs)

    monkeypatch.setattr(scan_mod, "run_scan", slow)

    r1 = client.post("/api/library/scan-fuzzy")
    r2 = client.post("/api/library/scan-fuzzy")
    assert r1.status_code == 200
    assert r2.status_code == 409


def test_scan_fuzzy_status_endpoint(client_with_album, tmp_path):
    client, _ = client_with_album
    client.app.state.db.set_config("downloads_path", str(tmp_path / "music"))
    (tmp_path / "music").mkdir()

    job_id = client.post("/api/library/scan-fuzzy").json()["job_id"]
    # TestClient runs the background task to completion synchronously via
    # the startup/shutdown lifespan if awaited; poll briefly.
    import time
    for _ in range(20):
        resp = client.get(f"/api/library/scan-fuzzy/{job_id}")
        if resp.json()["status"] == "complete":
            break
        time.sleep(0.05)
    body = resp.json()
    assert body["status"] == "complete"
    assert "auto_matched" in body
```

- [ ] **Step 2: Run — expect failure**

Run: `poetry run pytest tests/test_api_library_scan.py -v`
Expected: 404 on new endpoints.

- [ ] **Step 3: Implement the job registry + routes**

In `backend/main.py`, inside the `create_app` factory next to other `app.state.*` initialization, add:

```python
    app.state.scan_jobs = {}  # job_id → {"status": ..., "result": ...}
    app.state.active_scan_job = None  # one-at-a-time guard
```

In `backend/api/library.py` (use module-level import so tests can monkeypatch `scan.run_scan`):

```python
import uuid
import asyncio

from ..services import scan as scan_service


@router.post("/scan-fuzzy")
async def start_scan(request: Request):
    app = request.app
    if app.state.active_scan_job is not None:
        return JSONResponse(
            {"error": "Another scan is already running"}, status_code=409
        )

    db = app.state.db
    download_path = db.get_config("downloads_path") or "/music"
    sentinel_enabled = (
        (db.get_config("scan_sentinel_write_enabled") or "True") == "True"
    )

    job_id = uuid.uuid4().hex
    app.state.scan_jobs[job_id] = {"status": "running", "result": None}
    app.state.active_scan_job = job_id

    async def runner():
        try:
            result = await scan_service.run_scan(
                db,
                download_path=download_path,
                dedup_db_dir=_dedup_db_dir(),
                event_bus=app.state.event_bus,
                sentinel_write_enabled=sentinel_enabled,
            )
            app.state.scan_jobs[job_id] = {"status": "complete", "result": result}
        except Exception as e:
            app.state.scan_jobs[job_id] = {"status": "error", "result": {"error": str(e)}}
        finally:
            app.state.active_scan_job = None

    asyncio.create_task(runner())
    return {"job_id": job_id}


@router.get("/scan-fuzzy/{job_id}")
async def scan_status(request: Request, job_id: str):
    job = request.app.state.scan_jobs.get(job_id)
    if job is None:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    if job["status"] == "running":
        return {"status": "running"}
    if job["status"] == "error":
        return {"status": "error", **(job["result"] or {})}
    return {"status": "complete", **(job["result"] or {})}
```

- [ ] **Step 4: Run the tests — expect pass**

Run: `poetry run pytest tests/test_api_library_scan.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/api/library.py backend/main.py tests/test_api_library_scan.py
git commit -m "feat(api): scan-fuzzy start + status endpoints with single-job guard"
```

---

## Task 12: Frontend — ScanReview component

**Goal:** A slide-over panel with three sections (auto-matched / review / unmatched). Confirms review entries via the mark-downloaded endpoint.

**Files:**
- Create: `frontend/src/lib/components/ScanReview.svelte`

- [ ] **Step 1: Inspect existing panel/modal patterns**

Run: `grep -rn "slide-over\|modal\|overlay" frontend/src/lib/components/ | head`
Goal: pick the closest existing pattern (likely `AlbumDetail.svelte` — it's already used as a slide-over detail panel). Mirror its root element, close button, and backdrop styling.

- [ ] **Step 2: Write the component**

Create `frontend/src/lib/components/ScanReview.svelte`:

```svelte
<script lang="ts">
  interface Candidate {
    album_id: number;
    source: string;
    artist: string;
    title: string;
    score: number;
    reason: string;
  }

  interface ReviewEntry {
    folder: string;
    local_bit_depth: number | null;
    local_sample_rate: number | null;
    candidates: Candidate[];
  }

  interface ScanResult {
    status: 'running' | 'complete' | 'error';
    scanned?: number;
    total?: number;
    sentinel_skipped?: number;
    auto_matched?: { album_id: number; folder: string; reason: string }[];
    review?: ReviewEntry[];
    unmatched?: string[];
    error?: string;
  }

  let { result, onConfirm, onClose } = $props<{
    result: ScanResult;
    onConfirm: (albumId: number, folder: string) => Promise<void>;
    onClose: () => void;
  }>();

  let expanded = $state<{ auto: boolean; review: boolean; unmatched: boolean }>({
    auto: false, review: true, unmatched: false,
  });
  let processing = $state<Set<number>>(new Set());

  async function confirm(entry: ReviewEntry, candidate: Candidate) {
    processing = new Set([...processing, candidate.album_id]);
    try {
      await onConfirm(candidate.album_id, entry.folder);
      // Remove the entry from the list.
      if (result.review) {
        result.review = result.review.filter(e => e !== entry);
      }
    } finally {
      processing.delete(candidate.album_id);
      processing = new Set(processing);
    }
  }

  function copyUnmatched() {
    if (!result.unmatched) return;
    navigator.clipboard.writeText(result.unmatched.join('\n'));
  }
</script>

<div class="overlay" role="dialog" aria-labelledby="scan-review-title">
  <button class="backdrop" aria-label="Close" onclick={onClose}></button>
  <aside class="panel">
    <header>
      <h2 id="scan-review-title">Scan Results</h2>
      <button class="close" onclick={onClose} aria-label="Close">×</button>
    </header>

    {#if result.status === 'running'}
      <p>Scanning… {result.scanned ?? 0} / {result.total ?? '?'}</p>
    {:else if result.status === 'error'}
      <p class="error">Scan failed: {result.error}</p>
    {:else}
      <p class="summary">
        Scanned {result.scanned} folders ·
        {result.auto_matched?.length ?? 0} auto-matched ·
        {result.review?.length ?? 0} need review ·
        {result.unmatched?.length ?? 0} unmatched
        {#if result.sentinel_skipped}· {result.sentinel_skipped} already sentineled{/if}
      </p>

      <!-- Auto-matched -->
      <section>
        <button class="section-header"
                onclick={() => expanded.auto = !expanded.auto}>
          {expanded.auto ? '▾' : '▸'} Auto-matched ({result.auto_matched?.length ?? 0})
        </button>
        {#if expanded.auto && result.auto_matched}
          <ul class="list">
            {#each result.auto_matched as m}
              <li><code>{m.folder}</code> → album #{m.album_id}</li>
            {/each}
          </ul>
        {/if}
      </section>

      <!-- Review -->
      <section>
        <button class="section-header"
                onclick={() => expanded.review = !expanded.review}>
          {expanded.review ? '▾' : '▸'} Needs review ({result.review?.length ?? 0})
        </button>
        {#if expanded.review && result.review}
          <ul class="list">
            {#each result.review as entry}
              <li class="review-entry">
                <div class="folder"><code>{entry.folder}</code>
                  {#if entry.local_bit_depth}· local {entry.local_bit_depth}-bit{/if}
                </div>
                {#each entry.candidates as cand}
                  <div class="candidate">
                    <div>
                      <strong>{cand.artist}</strong> — {cand.title}
                      <span class="source">({cand.source})</span>
                      <div class="reason">{cand.reason}</div>
                    </div>
                    <button class="btn btn-secondary btn-sm"
                            disabled={processing.has(cand.album_id)}
                            onclick={() => confirm(entry, cand)}>
                      {processing.has(cand.album_id) ? '…' : 'Confirm'}
                    </button>
                  </div>
                {/each}
              </li>
            {/each}
          </ul>
        {/if}
      </section>

      <!-- Unmatched -->
      <section>
        <button class="section-header"
                onclick={() => expanded.unmatched = !expanded.unmatched}>
          {expanded.unmatched ? '▾' : '▸'} Unmatched folders ({result.unmatched?.length ?? 0})
        </button>
        {#if expanded.unmatched && result.unmatched}
          <div class="unmatched-actions">
            <button class="btn btn-secondary btn-sm" onclick={copyUnmatched}>
              Copy paths to clipboard
            </button>
          </div>
          <ul class="list">
            {#each result.unmatched as path}
              <li><code>{path}</code></li>
            {/each}
          </ul>
        {/if}
      </section>
    {/if}
  </aside>
</div>

<style>
  .overlay { position: fixed; inset: 0; z-index: 1000; }
  .backdrop {
    position: absolute; inset: 0; background: rgba(0,0,0,0.4);
    border: none; cursor: pointer;
  }
  .panel {
    position: absolute; top: 0; right: 0; bottom: 0; width: min(640px, 90vw);
    background: var(--surface); padding: var(--space-4);
    overflow-y: auto; box-shadow: -4px 0 0 var(--shadow-color, #000);
  }
  header {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: var(--space-3);
  }
  .close {
    background: none; border: none; font-size: 1.5rem; cursor: pointer;
    color: var(--fg);
  }
  .summary { color: var(--muted); font-size: var(--text-sm); }
  .section-header {
    background: none; border: none; font-family: inherit;
    font-size: var(--text-base); padding: var(--space-2) 0;
    cursor: pointer; text-align: left; width: 100%;
    border-top: 1px solid var(--border); color: var(--fg);
  }
  .list { list-style: none; padding: 0; margin: 0 0 var(--space-3) 0; }
  .list li { padding: var(--space-2) 0; border-bottom: 1px solid var(--border); }
  .review-entry .folder { margin-bottom: var(--space-1); }
  .candidate {
    display: flex; justify-content: space-between; gap: var(--space-2);
    padding: var(--space-1) 0;
  }
  .candidate .reason { font-size: var(--text-xs); color: var(--muted); }
  .candidate .source { font-size: var(--text-xs); color: var(--muted); }
  .unmatched-actions { margin-bottom: var(--space-2); }
  .error { color: var(--destructive); }
  code { font-family: var(--font-mono); font-size: var(--text-xs); }
</style>
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/lib/components/ScanReview.svelte
git commit -m "feat(frontend): ScanReview slide-over for scan results"
```

---

## Task 13: Frontend — wire Settings scan button + WS progress

**Goal:** The existing Settings "Scan Folder" button opens the `ScanReview` panel, polls the status endpoint (with WS progress driving the progress label), and wires confirm actions to `mark-downloaded`.

**Files:**
- Modify: `frontend/src/routes/settings/+page.svelte`
- Modify: `frontend/src/lib/api.ts` (or whatever the shared fetch module is — check during implementation)

- [ ] **Step 1: Locate the API client**

Run: `grep -rn "library.markDownloaded\|library:\s*{" frontend/src/lib/ | head`
If no `library` namespace exists, check the existing shape (`api.config`, `api.auth`) and follow the same pattern. Expect a file like `frontend/src/lib/api.ts` with a default-exported object.

- [ ] **Step 2: Extend the API client**

Add:

```typescript
export const library = {
  scanFuzzy: () => fetch('/api/library/scan-fuzzy', { method: 'POST' }).then(r => r.json()),
  scanFuzzyStatus: (jobId: string) =>
    fetch(`/api/library/scan-fuzzy/${jobId}`).then(r => r.json()),
  markDownloaded: (albumId: number, localFolderPath: string | null) =>
    fetch(`/api/library/albums/${albumId}/mark-downloaded`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ local_folder_path: localFolderPath }),
    }).then(r => r.json()),
  unmarkDownloaded: (albumId: number) =>
    fetch(`/api/library/albums/${albumId}/unmark-downloaded`, { method: 'POST' })
      .then(r => r.json()),
};
```

Export it from the default `api` object to match existing shape.

- [ ] **Step 3: Rewire the Settings scan button**

In `frontend/src/routes/settings/+page.svelte`, replace the existing `scanDownloads` implementation:

```javascript
  import ScanReview from '$lib/components/ScanReview.svelte';

  let scanOpen = $state(false);
  let scanResultState = $state<any>({ status: 'running', scanned: 0, total: 0 });
  let scanJobId = $state<string | null>(null);
  let scanPollTimer: ReturnType<typeof setInterval> | null = null;

  async function scanDownloads() {
    scanOpen = true;
    scanResultState = { status: 'running', scanned: 0, total: 0 };
    try {
      const { job_id } = await api.library.scanFuzzy();
      scanJobId = job_id;
      scanPollTimer = setInterval(async () => {
        if (!scanJobId) return;
        const data = await api.library.scanFuzzyStatus(scanJobId);
        scanResultState = data;
        if (data.status !== 'running') {
          if (scanPollTimer) clearInterval(scanPollTimer);
          scanPollTimer = null;
        }
      }, 500);
    } catch (e: any) {
      scanResultState = { status: 'error', error: e?.message ?? 'Scan failed' };
    }
  }

  async function onScanConfirm(albumId: number, folder: string) {
    await api.library.markDownloaded(albumId, folder);
  }

  function onScanClose() {
    scanOpen = false;
    if (scanPollTimer) clearInterval(scanPollTimer);
    scanPollTimer = null;
    scanJobId = null;
  }
```

Also subscribe to `scan_progress` events on the existing WebSocket store if it exists — purely cosmetic, same endpoint polling drives correctness. Skip if the WS store wiring is nontrivial.

Add at the bottom of the template:

```svelte
{#if scanOpen}
  <ScanReview result={scanResultState} onConfirm={onScanConfirm} onClose={onScanClose} />
{/if}
```

Remove the old `scanResult` text span now that the panel replaces it.

- [ ] **Step 4: Sanity check**

Can't run `svelte-check` without `npm install` in this env. At minimum:

Run: `node --test frontend/tests/*.test.js`
Expected: existing frontend logic tests still PASS.

Manual smoke check deferred to Task 15.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/routes/settings/+page.svelte frontend/src/lib/api.ts
git commit -m "feat(frontend): wire Settings scan button to ScanReview slide-over"
```

---

## Task 14: Frontend — AlbumDetail mark/unmark button

**Goal:** Add a button to the album detail panel that calls mark-downloaded (when not complete) or unmark-downloaded (when complete).

**Files:**
- Modify: `frontend/src/lib/components/AlbumDetail.svelte`

- [ ] **Step 1: Locate the download controls block**

Run: `grep -n "download\|status" frontend/src/lib/components/AlbumDetail.svelte | head -30`
Identify where the existing "Download" button is rendered — insert the new button adjacent to it.

- [ ] **Step 2: Add button logic**

In `AlbumDetail.svelte` script block, add:

```javascript
  import { api } from '$lib/api';

  let markLoading = $state(false);

  async function markOrUnmark() {
    if (!album) return;
    markLoading = true;
    try {
      if (album.download_status === 'complete') {
        const updated = await api.library.unmarkDownloaded(album.id);
        album = updated;
      } else {
        const updated = await api.library.markDownloaded(album.id, null);
        album = updated;
      }
    } finally {
      markLoading = false;
    }
  }
```

- [ ] **Step 3: Add the button markup**

Next to the existing Download button, add:

```svelte
<button class="btn btn-secondary btn-sm"
        disabled={markLoading}
        onclick={markOrUnmark}>
  {#if markLoading}
    …
  {:else if album.download_status === 'complete'}
    Unmark as downloaded
  {:else}
    Mark as downloaded
  {/if}
</button>
```

Style placement/aesthetic: mirror the existing secondary buttons in this file (same spacing, no icon).

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/components/AlbumDetail.svelte
git commit -m "feat(frontend): mark/unmark-downloaded button on album detail"
```

---

## Task 15: End-to-end verification against real library

**Goal:** With `make dev` running, point Libsync at the user's read-only music mount and run the scan. Confirm counts look reasonable, spot-check a handful of review / unmatched entries.

**This task is verification, not code.**

- [ ] **Step 1: Start dev server**

Run: `make dev`
Expected: backend logs `Uvicorn running on http://0.0.0.0:8080`.

- [ ] **Step 2: Configure**

Through the UI (http://localhost:8080/settings):
1. Set **Download Path** to the read-only mount point.
2. Toggle **Write sentinel file in album folders** OFF.
3. Save.

- [ ] **Step 3: Run the scan**

Click **Scan Folder**. Observe the slide-over counting up.

When complete, record:
- Total scanned
- Auto-matched count
- Needs-review count (and spot-check 3 entries — do reasons make sense?)
- Unmatched count (and spot-check 3 paths — are they truly absent from the library?)

- [ ] **Step 4: Spot-check mark/unmark**

Pick one already-complete album. Click **Unmark as downloaded** on its detail page. Refresh; verify the badge switches back to "not downloaded". Click **Mark as downloaded** to restore.

- [ ] **Step 5: Report back**

Summarize counts and any surprising classifications in the PR description. If classification heuristics need tuning, file follow-up issues — don't scope-creep this PR.

---

## Task 16: Final polish

- [ ] **Step 1: Full test run**

Run: `make test`
Expected: all tests PASS, including the new ones.

- [ ] **Step 2: Lint**

Run: `make lint`
Expected: clean (or only pre-existing warnings).

- [ ] **Step 3: Update docs**

Add a brief paragraph to `docs/WEB_UI.md` under the existing "Scan Downloads" section describing the new fuzzy-scan + mark-as-downloaded affordances. Keep it to ~6 lines.

- [ ] **Step 4: Commit + open PR**

```bash
git add docs/WEB_UI.md
git commit -m "docs: document fuzzy scan and mark-as-downloaded"
git push -u origin feature/library-scan-and-tidal-quality
gh pr create --title "Library scan: fuzzy matching + manual mark-as-downloaded (+ tidal quality fix)" ...
```

---

## Open follow-ups (out of scope)

- Local-only albums without a library match — currently just reported. Could later become full library entries.
- Track-level file-path recording.
- Learning from user-confirmed ambiguous matches (rule tuning stays manual).
- Renaming the `streamrip` → `libsync` internals (env var, DB filename) — tracked for v1.0.
