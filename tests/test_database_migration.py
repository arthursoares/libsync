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
