"""Atomic album/download-dedup reconciliation regressions."""

import sqlite3
from concurrent import futures
from datetime import datetime

import pytest

from backend.models.database import AlbumNotFoundError, AppDatabase
from backend.services.scan import mark_album_downloaded, unmark_album_downloaded

TRACK_IDS = ("t1", "t2", "t3")
NOW = datetime(2026, 9, 5, 12, 0, 0)


def _make_album(db: AppDatabase, source_album_id: str = "42") -> int:
    album_id = db.upsert_album(
        "qobuz",
        source_album_id,
        "Album",
        "Artist",
        track_count=len(TRACK_IDS),
    )
    for index, track_id in enumerate(TRACK_IDS, start=1):
        db.upsert_track(album_id, track_id, f"Track {index}", "Artist")
    return album_id


def _dedup_path(tmp_path):
    return tmp_path / "downloads.db"


def _create_dedup(path, ids=(), trigger_sql=None):
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE downloads (id TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO downloads (id) VALUES (?)", [(i,) for i in ids])
        if trigger_sql:
            conn.execute(trigger_sql)
        conn.commit()
    finally:
        conn.close()


def _dedup_ids(path):
    conn = sqlite3.connect(path)
    try:
        return {row[0] for row in conn.execute("SELECT id FROM downloads").fetchall()}
    finally:
        conn.close()


def _journal_mode(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()


def _album_state(db, album_id):
    album = db.get_album(album_id)
    assert album is not None
    return (
        album["download_status"],
        album["downloaded_at"],
        album["local_folder_path"],
    )


def test_dedup_insert_failure_rolls_back_album_and_all_inserted_ids(tmp_path):
    db = AppDatabase(str(tmp_path / "libsync.db"))
    album_id = _make_album(db)
    old_folder = str(tmp_path / "old")
    with db._connect() as conn:
        conn.execute(
            """UPDATE albums
               SET download_status = 'failed', downloaded_at = ?, local_folder_path = ?
               WHERE id = ?""",
            ("2026-01-01T00:00:00", old_folder, album_id),
        )
    dedup_path = _dedup_path(tmp_path)
    _create_dedup(
        dedup_path,
        ids=("unrelated",),
        trigger_sql="""CREATE TRIGGER fail_second_insert
                       BEFORE INSERT ON downloads WHEN NEW.id = 't2'
                       BEGIN SELECT RAISE(FAIL, 'dedup insert failed'); END""",
    )
    before = _album_state(db, album_id)

    with pytest.raises(sqlite3.IntegrityError, match="dedup insert failed"):
        mark_album_downloaded(
            db,
            album_id,
            local_folder_path=str(tmp_path / "new"),
            dedup_db_dir=str(tmp_path),
            track_ids=TRACK_IDS,
            sentinel_write_enabled=False,
            now=NOW,
        )

    assert _album_state(db, album_id) == before
    assert _dedup_ids(dedup_path) == {"unrelated"}


def test_app_update_failure_after_dedup_insert_rolls_back_both_databases(tmp_path):
    db = AppDatabase(str(tmp_path / "libsync.db"))
    album_id = _make_album(db)
    with db._connect() as conn:
        conn.execute(
            f"""CREATE TRIGGER fail_album_update
                BEFORE UPDATE OF download_status ON albums WHEN OLD.id = {album_id}
                BEGIN SELECT RAISE(FAIL, 'app update failed'); END"""
        )
    dedup_path = _dedup_path(tmp_path)
    _create_dedup(dedup_path, ids=("unrelated",))

    with pytest.raises(sqlite3.IntegrityError, match="app update failed"):
        mark_album_downloaded(
            db,
            album_id,
            local_folder_path=None,
            dedup_db_dir=str(tmp_path),
            track_ids=TRACK_IDS,
            sentinel_write_enabled=False,
            now=NOW,
        )

    assert _album_state(db, album_id) == ("not_downloaded", None, None)
    assert _dedup_ids(dedup_path) == {"unrelated"}


@pytest.mark.parametrize("failure_point", ["dedup_delete", "app_update"])
def test_unmark_failure_rolls_back_both_and_leaves_sentinel(tmp_path, failure_point):
    db = AppDatabase(str(tmp_path / "libsync.db"))
    album_id = _make_album(db)
    folder = tmp_path / "music" / "Album"
    folder.mkdir(parents=True)
    sentinel = folder / ".streamrip.json"
    sentinel.write_text("existing")
    db.set_album_download_state(
        album_id,
        downloaded_at="2026-01-01T00:00:00",
        local_folder_path=str(folder),
    )
    dedup_path = _dedup_path(tmp_path)
    trigger = None
    if failure_point == "dedup_delete":
        trigger = """CREATE TRIGGER fail_second_delete
                     BEFORE DELETE ON downloads WHEN OLD.id = 't2'
                     BEGIN SELECT RAISE(FAIL, 'dedup delete failed'); END"""
    _create_dedup(dedup_path, ids=(*TRACK_IDS, "unrelated"), trigger_sql=trigger)
    if failure_point == "app_update":
        with db._connect() as conn:
            conn.execute(
                f"""CREATE TRIGGER fail_album_update
                    BEFORE UPDATE OF download_status ON albums WHEN OLD.id = {album_id}
                    BEGIN SELECT RAISE(FAIL, 'app update failed'); END"""
            )

    with pytest.raises(sqlite3.IntegrityError):
        unmark_album_downloaded(
            db,
            album_id,
            dedup_db_dir=str(tmp_path),
            track_ids=TRACK_IDS,
        )

    assert _album_state(db, album_id) == (
        "complete",
        "2026-01-01T00:00:00",
        str(folder),
    )
    assert _dedup_ids(dedup_path) == {*TRACK_IDS, "unrelated"}
    assert sentinel.read_text() == "existing"


def test_repeated_mark_and_unmark_are_idempotent_and_keep_unrelated_rows(tmp_path):
    db = AppDatabase(str(tmp_path / "libsync.db"))
    album_id = _make_album(db)
    dedup_path = _dedup_path(tmp_path)
    _create_dedup(dedup_path, ids=("unrelated",))

    for _ in range(2):
        mark_album_downloaded(
            db,
            album_id,
            local_folder_path=None,
            dedup_db_dir=str(tmp_path),
            track_ids=TRACK_IDS,
            sentinel_write_enabled=False,
            now=NOW,
        )
    assert _dedup_ids(dedup_path) == {*TRACK_IDS, "unrelated"}

    for _ in range(2):
        unmark_album_downloaded(
            db,
            album_id,
            dedup_db_dir=str(tmp_path),
            track_ids=TRACK_IDS,
        )
    assert _album_state(db, album_id) == ("not_downloaded", None, None)
    assert _dedup_ids(dedup_path) == {"unrelated"}

    app_mode = _journal_mode(db.path)
    dedup_mode = _journal_mode(dedup_path)
    assert app_mode.lower() == "wal"
    assert dedup_mode.lower() == "wal"


def test_missing_dedup_database_is_initialized_and_historical_ids_are_removed(
    tmp_path,
):
    db = AppDatabase(str(tmp_path / "libsync.db"))
    album_id = _make_album(db)
    db.upsert_track(album_id, "historical", "Old", "Artist")
    db.set_album_download_state(album_id, downloaded_at="2026-01-01T00:00:00")

    unmark_album_downloaded(
        db,
        album_id,
        dedup_db_dir=str(tmp_path),
        track_ids=TRACK_IDS,
    )

    assert _album_state(db, album_id) == ("not_downloaded", None, None)
    assert _dedup_ids(_dedup_path(tmp_path)) == set()


def test_incompatible_dedup_database_errors_without_album_mutation(tmp_path):
    db = AppDatabase(str(tmp_path / "libsync.db"))
    album_id = _make_album(db)
    dedup_path = _dedup_path(tmp_path)
    conn = sqlite3.connect(dedup_path)
    try:
        conn.execute("CREATE TABLE downloads (wrong_column TEXT)")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(sqlite3.DatabaseError, match="no column named id"):
        mark_album_downloaded(
            db,
            album_id,
            local_folder_path=None,
            dedup_db_dir=str(tmp_path),
            track_ids=TRACK_IDS,
            sentinel_write_enabled=False,
            now=NOW,
        )

    assert _album_state(db, album_id) == ("not_downloaded", None, None)


@pytest.mark.parametrize(
    ("schema", "seed_sql", "error", "sentinel_contents"),
    [
        (
            "CREATE TABLE downloads (id TEXT PRIMARY KEY, required TEXT NOT NULL)",
            "INSERT INTO downloads (id, required) VALUES ('unrelated', 'keep')",
            "NOT NULL constraint failed",
            "existing",
        ),
        (
            "CREATE TABLE downloads (id TEXT PRIMARY KEY CHECK (id != 't2'))",
            "INSERT INTO downloads (id) VALUES ('unrelated')",
            "CHECK constraint failed",
            None,
        ),
        (
            "CREATE TABLE downloads (id TEXT)",
            "INSERT INTO downloads (id) VALUES ('unrelated')",
            "ON CONFLICT clause does not match",
            "existing",
        ),
    ],
)
def test_incompatible_existing_schema_rolls_back_without_touching_sentinel(
    tmp_path, schema, seed_sql, error, sentinel_contents
):
    db = AppDatabase(str(tmp_path / "libsync.db"))
    album_id = _make_album(db)
    old_folder = str(tmp_path / "old")
    with db._connect() as conn:
        conn.execute(
            """UPDATE albums
               SET download_status = 'failed', downloaded_at = ?, local_folder_path = ?
               WHERE id = ?""",
            ("2026-01-01T00:00:00", old_folder, album_id),
        )
    before = _album_state(db, album_id)

    dedup_path = _dedup_path(tmp_path)
    conn = sqlite3.connect(dedup_path)
    try:
        conn.execute(schema)
        conn.execute(seed_sql)
        conn.commit()
    finally:
        conn.close()

    folder = tmp_path / "music" / "Album"
    folder.mkdir(parents=True)
    sentinel = folder / ".streamrip.json"
    if sentinel_contents is not None:
        sentinel.write_text(sentinel_contents)

    with pytest.raises(sqlite3.DatabaseError, match=error):
        mark_album_downloaded(
            db,
            album_id,
            local_folder_path=str(folder),
            dedup_db_dir=str(tmp_path),
            track_ids=TRACK_IDS,
            sentinel_write_enabled=True,
            now=NOW,
        )

    assert _album_state(db, album_id) == before
    assert _dedup_ids(dedup_path) == {"unrelated"}
    if sentinel_contents is None:
        assert not sentinel.exists()
    else:
        assert sentinel.read_text() == sentinel_contents


def test_missing_album_is_rejected_after_transaction_starts(tmp_path):
    db = AppDatabase(str(tmp_path / "libsync.db"))

    with pytest.raises(AlbumNotFoundError, match="Album 999 not found"):
        db.apply_album_download_state(
            999,
            True,
            TRACK_IDS,
            str(_dedup_path(tmp_path)),
            NOW.isoformat(),
            None,
        )

    assert db.get_albums("qobuz") == []


def test_memory_database_connection_is_serialized_across_worker_threads(tmp_path):
    db = AppDatabase(":memory:")
    first = _make_album(db, "first")
    second = _make_album(db, "second")

    def mark(album_id):
        mark_album_downloaded(
            db,
            album_id,
            local_folder_path=None,
            dedup_db_dir=str(tmp_path),
            track_ids=(f"album-{album_id}",),
            sentinel_write_enabled=False,
            now=NOW,
        )

    with futures.ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(mark, (first, second)))

    first_album = db.get_album(first)
    second_album = db.get_album(second)
    assert first_album is not None
    assert second_album is not None
    assert first_album["download_status"] == "complete"
    assert second_album["download_status"] == "complete"
    assert _dedup_ids(_dedup_path(tmp_path)) == {
        f"album-{first}",
        f"album-{second}",
    }


def test_attached_path_is_parameterized_and_tidal_uses_its_own_database(tmp_path):
    dedup_dir = tmp_path / "user's data"
    db = AppDatabase(str(tmp_path / "libsync.db"))
    album_id = db.upsert_album("tidal", "tidal-1", "Album", "Artist", track_count=1)

    mark_album_downloaded(
        db,
        album_id,
        local_folder_path=None,
        dedup_db_dir=str(dedup_dir),
        track_ids=("tidal-track",),
        sentinel_write_enabled=False,
        now=NOW,
    )

    tidal_path = dedup_dir / "downloads-tidal.db"
    assert _dedup_ids(tidal_path) == {"tidal-track"}
    assert not (dedup_dir / "downloads.db").exists()
