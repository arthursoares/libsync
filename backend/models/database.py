"""Extended SQLite database with WAL mode for the web application."""

import logging
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

logger = logging.getLogger("streamrip")

SCHEMA_VERSION = 2


class AlbumDownloadStateError(RuntimeError):
    """Album/dedup reconciliation could not be applied."""

    status_code = 500


class AlbumNotFoundError(AlbumDownloadStateError):
    """The album disappeared before reconciliation acquired its transaction."""

    status_code = 404


class AlbumDownloadStateConflictError(AlbumDownloadStateError):
    """An in-flight download owns the album state."""

    status_code = 409


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_album_id TEXT NOT NULL,
    title TEXT NOT NULL,
    artist TEXT NOT NULL,
    release_date TEXT,
    label TEXT,
    genre TEXT,
    track_count INTEGER,
    duration_seconds INTEGER,
    cover_url TEXT,
    cover_path TEXT,
    quality TEXT,
    bit_depth INTEGER,
    sample_rate REAL,
    local_folder_path TEXT,
    file_size_bytes INTEGER,
    download_status TEXT NOT NULL DEFAULT 'not_downloaded',
    downloaded_at TEXT,
    added_to_library_at TEXT,
    user_id INTEGER NOT NULL DEFAULT 1,
    UNIQUE(source, source_album_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_albums_source_user ON albums(source, user_id);
CREATE INDEX IF NOT EXISTS idx_albums_download_status ON albums(download_status);

CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    source_track_id TEXT NOT NULL,
    title TEXT NOT NULL,
    artist TEXT NOT NULL,
    track_number INTEGER,
    disc_number INTEGER DEFAULT 1,
    duration_seconds INTEGER,
    explicit BOOLEAN DEFAULT FALSE,
    isrc TEXT,
    format TEXT,
    bit_depth INTEGER,
    sample_rate INTEGER,
    file_path TEXT,
    download_status TEXT NOT NULL DEFAULT 'not_downloaded',
    UNIQUE(album_id, source_track_id)
);

CREATE INDEX IF NOT EXISTS idx_tracks_album ON tracks(album_id);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    albums_found INTEGER DEFAULT 0,
    albums_new INTEGER DEFAULT 0,
    albums_removed INTEGER DEFAULT 0,
    albums_downloaded INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);
"""


# Shared by upsert_album and upsert_albums so the column list and the
# ON CONFLICT merge rules are defined in exactly one place (#25).
_UPSERT_ALBUM_SQL = """INSERT INTO albums
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
"""


def _album_upsert_params(
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
) -> tuple:
    """Build the parameter tuple for `_UPSERT_ALBUM_SQL`, in column order."""
    return (
        source,
        source_album_id,
        title,
        artist,
        release_date,
        label,
        genre,
        track_count,
        duration_seconds,
        cover_url,
        quality,
        bit_depth,
        sample_rate,
        added_to_library_at,
        user_id,
    )


class AppDatabase:
    """Extended database for the web application."""

    def __init__(self, path: str):
        self.path = path
        self._connection_lock = threading.RLock()
        self._sqlite_timeout = 30.0
        self._persistent_conn: sqlite3.Connection | None = None
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        else:
            # In-memory databases are per-connection; keep one persistent
            # connection so the schema and data survive across calls.
            self._persistent_conn = sqlite3.connect(path, check_same_thread=False)
            self._persistent_conn.execute("PRAGMA foreign_keys=ON")
            self._persistent_conn.row_factory = sqlite3.Row
        self._init_schema()

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
            if current >= SCHEMA_VERSION:
                return
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

    @contextmanager
    def _connect(self):
        if self._persistent_conn is not None:
            # Yield the persistent connection without closing it.
            # Every :memory: caller shares this object, including worker-thread
            # scan mutations, so connection use must be serialized.
            with self._connection_lock:
                conn = self._persistent_conn
                try:
                    yield conn
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
        else:
            conn = sqlite3.connect(self.path, timeout=self._sqlite_timeout)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    # ── Albums ──

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
                _UPSERT_ALBUM_SQL,
                _album_upsert_params(
                    source,
                    source_album_id,
                    title,
                    artist,
                    release_date,
                    label,
                    genre,
                    track_count,
                    duration_seconds,
                    cover_url,
                    quality,
                    bit_depth,
                    sample_rate,
                    added_to_library_at,
                    user_id,
                ),
            )
            row = conn.execute(
                "SELECT id FROM albums WHERE source=? AND source_album_id=? AND user_id=?",
                (source, source_album_id, user_id),
            ).fetchone()
            return row["id"]

    def upsert_albums(self, rows: list[dict]) -> None:
        """Upsert many albums in one connection/transaction.

        Runs the same INSERT ... ON CONFLICT statement as `upsert_album`
        for every row, via `executemany` inside a single `_connect()`
        block, instead of opening one SQLite connection per album on the
        event loop (#25). Each dict in `rows` uses the same keyword names
        as `upsert_album`'s parameters; only `source`, `source_album_id`,
        `title`, and `artist` are required.
        """
        if not rows:
            return
        params = [
            _album_upsert_params(
                row["source"],
                row["source_album_id"],
                row["title"],
                row["artist"],
                row.get("release_date"),
                row.get("label"),
                row.get("genre"),
                row.get("track_count"),
                row.get("duration_seconds"),
                row.get("cover_url"),
                row.get("quality"),
                row.get("bit_depth"),
                row.get("sample_rate"),
                row.get("added_to_library_at"),
                row.get("user_id", 1),
            )
            for row in rows
        ]
        with self._connect() as conn:
            conn.executemany(_UPSERT_ALBUM_SQL, params)

    def get_albums(
        self,
        source: str,
        user_id: int = 1,
        status: str | None = None,
        search: str | None = None,
        sort_by: str = "added_to_library_at",
        sort_dir: str = "DESC",
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        conditions = ["source = ?", "user_id = ?"]
        params: list = [source, user_id]

        if status and status != "all":
            conditions.append("download_status = ?")
            params.append(status)

        if search:
            conditions.append("(title LIKE ? OR artist LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])

        allowed_sorts = {
            "added_to_library_at",
            "title",
            "artist",
            "release_date",
            "downloaded_at",
        }
        if sort_by not in allowed_sorts:
            sort_by = "added_to_library_at"
        if sort_dir not in ("ASC", "DESC"):
            sort_dir = "DESC"

        where = " AND ".join(conditions)
        # NULLS LAST ensures albums without added_to_library_at sort to the bottom
        nulls = "NULLS LAST" if sort_dir == "DESC" else "NULLS FIRST"
        query = f"""
            SELECT * FROM albums
            WHERE {where}
            ORDER BY {sort_by} {sort_dir} {nulls}
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_album(self, album_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM albums WHERE id = ?", (album_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_album_by_source_id(
        self, source: str, source_album_id: str, user_id: int = 1
    ) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM albums WHERE source=? AND source_album_id=? AND user_id=?",
                (source, source_album_id, user_id),
            ).fetchone()
            return dict(row) if row else None

    def get_recent_downloads(self, limit: int = 50) -> list[dict]:
        """Get recently downloaded/failed albums for download history."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM albums
                   WHERE download_status IN ('complete', 'failed')
                   AND downloaded_at IS NOT NULL
                   ORDER BY downloaded_at DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def update_album_status(
        self, album_id: int, status: str, downloaded_at: str | None = None
    ):
        with self._connect() as conn:
            if downloaded_at:
                conn.execute(
                    "UPDATE albums SET download_status=?, downloaded_at=? WHERE id=?",
                    (status, downloaded_at, album_id),
                )
            else:
                conn.execute(
                    "UPDATE albums SET download_status=? WHERE id=?",
                    (status, album_id),
                )

    def reset_transient_download_statuses(self) -> int:
        """Reset albums stuck in 'queued' or 'downloading' back to
        'not_downloaded'.

        The download queue is in-memory only (D7 in the architecture
        contract); a backend restart forgets queued and in-flight items
        but leaves the corresponding albums' `download_status` untouched,
        so the UI shows a permanent spinner (#32). Called once at startup
        in `backend.main.create_app`, right after the database is opened.
        Does not re-enqueue anything.

        Returns the number of rows reset.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE albums SET download_status = 'not_downloaded'
                   WHERE download_status IN ('queued', 'downloading')"""
            )
            return cursor.rowcount

    def update_album_resolved_metadata(
        self,
        album_id: int,
        title: str,
        artist: str,
        track_count: int | None = None,
    ) -> None:
        """Write back the title/artist/track_count a download resolved.

        Deliberately narrow: the download path only learns these three
        fields, so it must not go through ``upsert_album``, whose
        ``DO UPDATE`` overwrites every other metadata column with the
        ``None`` of an omitted kwarg — wiping cover_url, release_date,
        label, genre, duration_seconds and quality off a downloaded album.
        Sync legitimately relies on that overwrite behaviour, so the fix
        belongs here rather than in ``upsert_album``.
        """
        with self._connect() as conn:
            conn.execute(
                """UPDATE albums
                   SET title = ?, artist = ?,
                       track_count = COALESCE(?, track_count)
                   WHERE id = ?""",
                (title, artist, track_count, album_id),
            )

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

    def apply_album_download_state(
        self,
        album_id: int,
        downloaded: bool,
        track_ids: tuple[str, ...],
        dedup_db_path: str,
        downloaded_at: str | None,
        local_folder_path: str | None,
    ) -> dict:
        """Reconcile album and per-source dedup state in one transaction.

        The dedup database is attached to the app connection so ordinary SQL,
        locking, and application failures roll both databases back together.
        This does not promise crash atomicity across separate WAL files.
        Returns the album snapshot read after the write transaction starts;
        callers use its old folder for post-commit sentinel cleanup.
        """
        if not track_ids:
            raise ValueError("A complete non-empty track identity set is required")
        if downloaded and not downloaded_at:
            raise ValueError("downloaded_at is required when marking an album")
        if not isinstance(dedup_db_path, str) or not dedup_db_path.strip():
            raise ValueError("dedup_db_path must be a filesystem path")
        if "\x00" in dedup_db_path or dedup_db_path == ":memory:":
            raise ValueError("dedup_db_path must be a filesystem path")

        dedup_path = os.path.abspath(dedup_db_path)
        if self.path != ":memory:" and os.path.realpath(dedup_path) == os.path.realpath(
            os.path.abspath(self.path)
        ):
            raise ValueError("The dedup database must differ from the app database")
        os.makedirs(os.path.dirname(dedup_path) or ".", exist_ok=True)

        persistent = self._persistent_conn is not None
        lock = self._connection_lock
        lock.acquire()
        conn: sqlite3.Connection | None = None
        attached = False
        try:
            conn = self._persistent_conn
            if conn is None:
                conn = sqlite3.connect(
                    self.path,
                    timeout=self._sqlite_timeout,
                    isolation_level=None,
                )
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.row_factory = sqlite3.Row

            conn.execute(
                "ATTACH DATABASE ? AS download_state_dedup",
                (dedup_path,),
            )
            attached = True
            conn.execute("PRAGMA download_state_dedup.journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM albums WHERE id = ?", (album_id,)
                ).fetchone()
                if row is None:
                    raise AlbumNotFoundError(f"Album {album_id} not found")
                album = dict(row)
                if album["download_status"] in {"queued", "downloading"}:
                    raise AlbumDownloadStateConflictError(
                        f"Album {album_id} is currently {album['download_status']}"
                    )

                expected_name = (
                    "downloads.db"
                    if album["source"] == "qobuz"
                    else f"downloads-{album['source']}.db"
                )
                if os.path.basename(dedup_path) != expected_name:
                    raise ValueError(
                        f"Expected {expected_name} for {album['source']} dedup state"
                    )

                conn.execute(
                    """CREATE TABLE IF NOT EXISTS
                       download_state_dedup.downloads (id TEXT PRIMARY KEY)"""
                )

                reconciled_ids = list(dict.fromkeys(str(tid) for tid in track_ids))
                if downloaded:
                    conn.executemany(
                        """INSERT INTO download_state_dedup.downloads (id)
                           VALUES (?) ON CONFLICT(id) DO NOTHING""",
                        [(track_id,) for track_id in reconciled_ids],
                    )
                    cursor = conn.execute(
                        """UPDATE albums
                           SET download_status = 'complete',
                               downloaded_at = ?,
                               local_folder_path = COALESCE(?, local_folder_path)
                           WHERE id = ?""",
                        (downloaded_at, local_folder_path, album_id),
                    )
                else:
                    historical_ids = [
                        row["source_track_id"]
                        for row in conn.execute(
                            "SELECT source_track_id FROM tracks WHERE album_id = ?",
                            (album_id,),
                        ).fetchall()
                    ]
                    reconciled_ids = list(
                        dict.fromkeys((*reconciled_ids, *historical_ids))
                    )
                    conn.executemany(
                        "DELETE FROM download_state_dedup.downloads WHERE id = ?",
                        [(track_id,) for track_id in reconciled_ids],
                    )
                    cursor = conn.execute(
                        """UPDATE albums
                           SET download_status = 'not_downloaded',
                               downloaded_at = NULL,
                               local_folder_path = NULL
                           WHERE id = ?""",
                        (album_id,),
                    )

                if cursor.rowcount != 1:
                    raise AlbumNotFoundError(f"Album {album_id} not found")
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            try:
                if attached and conn is not None:
                    conn.execute("DETACH DATABASE download_state_dedup")
            finally:
                try:
                    if not persistent and conn is not None:
                        conn.close()
                finally:
                    lock.release()

        return album

    def count_albums(
        self,
        source: str,
        user_id: int = 1,
        status: str | None = None,
        search: str | None = None,
    ) -> int:
        conditions = ["source = ?", "user_id = ?"]
        params: list = [source, user_id]
        # "all" is the no-filter sentinel; it must be interpreted exactly as
        # get_albums does, or pagination totals read 0 for a page of rows.
        if status and status != "all":
            conditions.append("download_status = ?")
            params.append(status)
        if search:
            conditions.append("(title LIKE ? OR artist LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        where = " AND ".join(conditions)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) as cnt FROM albums WHERE {where}", params
            ).fetchone()
            return row["cnt"]

    # ── Tracks ──

    def upsert_track(
        self,
        album_id: int,
        source_track_id: str,
        title: str,
        artist: str,
        track_number: int | None = None,
        disc_number: int = 1,
        duration_seconds: int | None = None,
        explicit: bool = False,
        isrc: str | None = None,
    ) -> int:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO tracks
                   (album_id, source_track_id, title, artist, track_number,
                    disc_number, duration_seconds, explicit, isrc)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(album_id, source_track_id)
                   DO UPDATE SET
                     title=excluded.title, artist=excluded.artist,
                     track_number=excluded.track_number,
                     disc_number=excluded.disc_number,
                     duration_seconds=excluded.duration_seconds,
                     explicit=excluded.explicit, isrc=excluded.isrc
                """,
                (
                    album_id,
                    source_track_id,
                    title,
                    artist,
                    track_number,
                    disc_number,
                    duration_seconds,
                    explicit,
                    isrc,
                ),
            )
            row = conn.execute(
                "SELECT id FROM tracks WHERE album_id=? AND source_track_id=?",
                (album_id, source_track_id),
            ).fetchone()
            return row["id"]

    def cache_album_tracks(
        self,
        album_id: int,
        tracks: list[dict],
        *,
        authoritative_count: int,
    ) -> None:
        """Cache a complete catalog response and its count in one transaction.

        Existing rows are retained so historical identities remain available
        for unmark reconciliation. Conflict updates touch catalog metadata only;
        download status and local file metadata are deliberately preserved.
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE albums SET track_count = ? WHERE id = ?",
                (authoritative_count, album_id),
            )
            conn.executemany(
                """INSERT INTO tracks
                   (album_id, source_track_id, title, artist, track_number,
                    disc_number, duration_seconds, explicit, isrc)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(album_id, source_track_id)
                   DO UPDATE SET
                     title=excluded.title, artist=excluded.artist,
                     track_number=excluded.track_number,
                     disc_number=excluded.disc_number,
                     duration_seconds=excluded.duration_seconds,
                     explicit=excluded.explicit, isrc=excluded.isrc
                """,
                [
                    (
                        album_id,
                        track["source_track_id"],
                        track["title"],
                        track["artist"],
                        track.get("track_number"),
                        track.get("disc_number", 1),
                        track.get("duration_seconds"),
                        track.get("explicit", False),
                        track.get("isrc"),
                    )
                    for track in tracks
                ],
            )

    def get_tracks(self, album_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tracks WHERE album_id=? ORDER BY disc_number, track_number",
                (album_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def update_track_status(
        self,
        track_id: int,
        status: str,
        file_path: str | None = None,
        format: str | None = None,
        bit_depth: int | None = None,
        sample_rate: int | None = None,
    ):
        # The metadata columns are COALESCEd so a status-only call (the sole
        # caller in DownloadService passes just a status) doesn't NULL the
        # file_path/format/bit_depth/sample_rate recorded by an earlier write.
        with self._connect() as conn:
            conn.execute(
                """UPDATE tracks SET download_status=?,
                   file_path=COALESCE(?, file_path),
                   format=COALESCE(?, format),
                   bit_depth=COALESCE(?, bit_depth),
                   sample_rate=COALESCE(?, sample_rate)
                   WHERE id=?""",
                (status, file_path, format, bit_depth, sample_rate, track_id),
            )

    # ── Sync Runs ──

    def create_sync_run(self, source: str) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO sync_runs (source, started_at) VALUES (?, ?)",
                (source, datetime.now().isoformat()),
            )
            return cursor.lastrowid

    def complete_sync_run(
        self,
        run_id: int,
        albums_found: int,
        albums_new: int,
        albums_removed: int,
        albums_downloaded: int,
    ):
        with self._connect() as conn:
            conn.execute(
                """UPDATE sync_runs SET completed_at=?, albums_found=?,
                   albums_new=?, albums_removed=?, albums_downloaded=?,
                   status='complete'
                   WHERE id=?""",
                (
                    datetime.now().isoformat(),
                    albums_found,
                    albums_new,
                    albums_removed,
                    albums_downloaded,
                    run_id,
                ),
            )

    def fail_sync_run(self, run_id: int):
        with self._connect() as conn:
            conn.execute(
                """UPDATE sync_runs SET completed_at=?, status='failed'
                   WHERE id=?""",
                (datetime.now().isoformat(), run_id),
            )

    def get_sync_history(self, source: str, limit: int = 10) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sync_runs WHERE source=? ORDER BY started_at DESC LIMIT ?",
                (source, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Config ──

    def get_config(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM config WHERE key=?", (key,)
            ).fetchone()
            return row["value"] if row else None

    def set_config(self, key: str, value: str):
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO config (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (key, value, datetime.now().isoformat()),
            )

    def get_all_config(self) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM config").fetchall()
            return {r["key"]: r["value"] for r in rows}
