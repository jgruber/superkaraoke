"""
SQLite database via aiosqlite.

Tables:
  songs      — persistent song library (title, artist, year, genre, likes, …)
  song_likes — legacy table kept for migration only
"""
import logging
import aiosqlite
from typing import Optional
from .config import settings

log = logging.getLogger(__name__)
_DB = str(settings.db_path)


# ── Schema ─────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS songs (
    id               TEXT PRIMARY KEY,
    file_path        TEXT NOT NULL,
    cdg_path         TEXT,
    kind             TEXT NOT NULL CHECK(kind IN ('cdg','video')),
    title            TEXT NOT NULL DEFAULT '',
    artist           TEXT NOT NULL DEFAULT '',
    year             INTEGER,
    genre            TEXT NOT NULL DEFAULT '',
    likes            INTEGER NOT NULL DEFAULT 0,
    metadata_locked  INTEGER NOT NULL DEFAULT 0,
    is_duplicate     INTEGER NOT NULL DEFAULT 0,
    metadata_updated INTEGER NOT NULL DEFAULT 0,
    duration_secs    REAL,
    added_at         TEXT NOT NULL DEFAULT (datetime('now')),
    scanned_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_songs_title  ON songs(title  COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_songs_artist ON songs(artist COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_songs_likes  ON songs(likes  DESC);
CREATE INDEX IF NOT EXISTS idx_songs_kind   ON songs(kind);

-- Legacy table — kept so old DBs don't error; no longer written to.
CREATE TABLE IF NOT EXISTS song_likes (
    song_id TEXT PRIMARY KEY,
    count   INTEGER NOT NULL DEFAULT 0
);
"""


async def init_db():
    async with aiosqlite.connect(_DB) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.executescript(_DDL)
        # Migrate likes from legacy table into songs (if songs table was just created)
        await db.execute("""
            UPDATE songs
            SET likes = (SELECT count FROM song_likes WHERE song_id = songs.id)
            WHERE id IN (SELECT song_id FROM song_likes)
              AND metadata_locked = 0
        """)
        # Add is_duplicate column to existing databases that predate it
        try:
            await db.execute(
                "ALTER TABLE songs ADD COLUMN is_duplicate INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass  # column already exists
        # Add duration_secs column to existing databases that predate it
        try:
            await db.execute("ALTER TABLE songs ADD COLUMN duration_secs REAL")
        except Exception:
            pass  # column already exists
        # Add metadata_updated column to existing databases that predate it
        try:
            await db.execute(
                "ALTER TABLE songs ADD COLUMN metadata_updated INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass  # column already exists
        await db.commit()


# ── Internal helpers ───────────────────────────────────────────────────────────

def _row_to_song(row: aiosqlite.Row) -> dict:
    d = dict(row)
    d['path'] = d['file_path']   # alias expected by stream_manager / queue_manager
    return d


# ── Song CRUD ──────────────────────────────────────────────────────────────────

async def upsert_song(data: dict) -> dict:
    """
    Insert a new song with auto-detected metadata, or update file paths
    for an existing song (metadata is never overwritten here — use
    update_song_metadata() for that).
    """
    sid = data['id']
    async with aiosqlite.connect(_DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id FROM songs WHERE id = ?", (sid,)) as cur:
            exists = await cur.fetchone()

        if exists is None:
            # Check legacy likes table for migrated count
            async with db.execute(
                "SELECT count FROM song_likes WHERE song_id = ?", (sid,)
            ) as cur:
                legacy = await cur.fetchone()
            likes = legacy[0] if legacy else data.get('likes', 0)

            await db.execute("""
                INSERT INTO songs
                    (id, file_path, cdg_path, kind, title, artist, year, genre, likes,
                     duration_secs, scanned_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """, (
                sid,
                data['file_path'],
                data.get('cdg_path'),
                data['kind'],
                data.get('title', ''),
                data.get('artist', ''),
                data.get('year'),
                data.get('genre', ''),
                likes,
                data.get('duration_secs'),
            ))
        else:
            # Keep metadata intact; only refresh file paths and scan timestamp
            await db.execute("""
                UPDATE songs
                SET file_path = ?, cdg_path = ?, kind = ?, scanned_at = datetime('now')
                WHERE id = ?
            """, (data['file_path'], data.get('cdg_path'), data['kind'], sid))

        await db.commit()
        async with db.execute("SELECT * FROM songs WHERE id = ?", (sid,)) as cur:
            row = await cur.fetchone()
        return _row_to_song(row)


async def get_all_song_ids() -> set[str]:
    """Return the set of all song IDs currently in the DB (for scan diffing)."""
    async with aiosqlite.connect(_DB) as db:
        async with db.execute("SELECT id FROM songs") as cur:
            rows = await cur.fetchall()
    return {row[0] for row in rows}


async def touch_song_paths(song_id: str, file_path: str, cdg_path: Optional[str], kind: str):
    """Fast update for known songs: refresh file paths only, no metadata read needed."""
    async with aiosqlite.connect(_DB) as db:
        await db.execute("""
            UPDATE songs
            SET file_path = ?, cdg_path = ?, kind = ?, scanned_at = datetime('now')
            WHERE id = ?
        """, (file_path, cdg_path, kind, song_id))
        await db.commit()


async def bulk_upsert_songs(songs: list[dict]):
    """
    Insert many new songs in a single transaction.
    Skips any song_id that already exists (existing songs go through touch_song_paths).
    """
    if not songs:
        return
    async with aiosqlite.connect(_DB) as db:
        # Enable WAL for concurrent read access during bulk write
        await db.execute("PRAGMA journal_mode=WAL")
        await db.executemany("""
            INSERT OR IGNORE INTO songs
                (id, file_path, cdg_path, kind, title, artist, year, genre, likes,
                 duration_secs, scanned_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, datetime('now'))
        """, [
            (
                s["id"],
                s["file_path"],
                s.get("cdg_path"),
                s["kind"],
                s.get("title", ""),
                s.get("artist", ""),
                s.get("year"),
                s.get("genre", ""),
                s.get("duration_secs"),
            )
            for s in songs
        ])
        await db.commit()


async def get_song(song_id: str) -> Optional[dict]:
    async with aiosqlite.connect(_DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM songs WHERE id = ?", (song_id,)) as cur:
            row = await cur.fetchone()
    return _row_to_song(row) if row else None


async def search_songs(
    query: str = "",
    sort: str = "title",
    limit: int = 50,
    offset: int = 0,
    kind_filter: str = "",
    include_duplicates: bool = True,
) -> tuple[list[dict], int]:
    # Split query into words so "Elvis Blue" matches title "Blue Suede Shoes"
    # by artist "Elvis Presley" — each word must appear in title, artist, or genre.
    words = [w for w in query.strip().split() if w] if query else []
    if not words:
        words = [""]   # empty query → match everything

    conditions = [
        " AND ".join("(title LIKE ? OR artist LIKE ? OR genre LIKE ?)" for _ in words)
    ]
    base_params: list = []
    for w in words:
        wq = f"%{w}%"
        base_params.extend([wq, wq, wq])

    if kind_filter in ("cdg", "video"):
        conditions.append("kind = ?")
        base_params.append(kind_filter)

    if not include_duplicates:
        conditions.append("is_duplicate = 0")
        conditions.append("artist != ''")

    where = " AND ".join(conditions)
    order = {
        "title":  "title COLLATE NOCASE ASC, artist COLLATE NOCASE ASC",
        "artist": "artist COLLATE NOCASE ASC, title COLLATE NOCASE ASC",
        "likes":  "likes DESC, title COLLATE NOCASE ASC",
        "year":   "year DESC, title COLLATE NOCASE ASC",
        "genre":  "genre COLLATE NOCASE ASC, title COLLATE NOCASE ASC",
    }.get(sort, "title COLLATE NOCASE ASC")

    async with aiosqlite.connect(_DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT COUNT(*) FROM songs WHERE {where}", base_params
        ) as cur:
            total = (await cur.fetchone())[0]

        async with db.execute(
            f"SELECT * FROM songs WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
            [*base_params, limit, offset],
        ) as cur:
            rows = await cur.fetchall()

    return [_row_to_song(r) for r in rows], total


async def count_songs() -> int:
    async with aiosqlite.connect(_DB) as db:
        async with db.execute("SELECT COUNT(*) FROM songs") as cur:
            return (await cur.fetchone())[0]


async def get_library_stats() -> dict:
    async with aiosqlite.connect(_DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(kind = 'cdg'), 0) AS cdg_count,
                COALESCE(SUM(kind = 'video'), 0) AS video_count,
                COALESCE(SUM(artist = ''), 0) AS no_artist,
                COALESCE(SUM(likes > 0), 0) AS liked,
                COALESCE(SUM(is_duplicate = 1), 0) AS duplicate_count
            FROM songs
        """) as cur:
            row = await cur.fetchone()
    return dict(row) if row else {}


_DESTRUCTIVE_META_FIELDS = {"title", "artist", "year", "genre", "is_duplicate"}


async def update_song_metadata(
    song_id: str, fields: dict, *, force: bool = False,
) -> Optional[dict]:
    """
    Update user-editable metadata fields.

    Lock protection:
      - If the stored row has metadata_locked=1 AND the caller did NOT pass
        `metadata_locked` in `fields` AND `force` is False, then title,
        artist, year, genre and is_duplicate are DROPPED from the update
        (likes may still change). This is the safety net so drive-by
        writes from code that is not lock-aware cannot clobber curated
        metadata. The UI save() sends `metadata_locked` so user edits
        from the modal still work normally.

      - If `metadata_locked` is not in `fields`, default to locking (1) so
        an explicit edit protects against rescan overwrites.
    """
    allowed = {
        "title", "artist", "year", "genre", "likes",
        "metadata_locked", "is_duplicate", "metadata_updated",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return await get_song(song_id)

    existing = await get_song(song_id)
    if not existing:
        return None

    caller_wrote_lock = "metadata_locked" in updates
    if existing.get("metadata_locked") and not caller_wrote_lock and not force:
        dropped = {k: v for k, v in updates.items() if k in _DESTRUCTIVE_META_FIELDS}
        if dropped:
            log.warning(
                "update_song_metadata: honoring lock on %s (%r by %r); "
                "dropped %s. Pass force=True or include metadata_locked "
                "to override.",
                song_id, existing.get("title"), existing.get("artist"), dropped,
            )
            updates = {k: v for k, v in updates.items() if k not in _DESTRUCTIVE_META_FIELDS}
        if not updates:
            return existing

    if "metadata_locked" not in updates:
        updates["metadata_locked"] = 1

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [song_id]

    async with aiosqlite.connect(_DB) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            f"UPDATE songs SET {set_clause} WHERE id = ?", values
        )
        await db.commit()
        async with db.execute("SELECT * FROM songs WHERE id = ?", (song_id,)) as cur:
            row = await cur.fetchone()
    return _row_to_song(row) if row else None


async def redetect_song_metadata(
    song_id: str, metadata: dict, *, force: bool = False,
) -> Optional[dict]:
    """
    Apply freshly-detected metadata and clear the lock.
    This is an explicit destructive action — if the stored row is locked
    and `force` is False, the call is refused (returns the existing row
    unchanged).
    """
    existing = await get_song(song_id)
    if not existing:
        return None
    if existing.get("metadata_locked") and not force:
        log.warning(
            "redetect_song_metadata: refusing lock bypass on %s (%r by %r). "
            "Pass force=True to override.",
            song_id, existing.get("title"), existing.get("artist"),
        )
        return existing

    async with aiosqlite.connect(_DB) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("""
            UPDATE songs
            SET title = ?, artist = ?, year = ?, genre = ?,
                metadata_locked = 0, scanned_at = datetime('now')
            WHERE id = ?
        """, (
            metadata.get('title', ''),
            metadata.get('artist', ''),
            metadata.get('year'),
            metadata.get('genre', ''),
            song_id,
        ))
        await db.commit()
        async with db.execute("SELECT * FROM songs WHERE id = ?", (song_id,)) as cur:
            row = await cur.fetchone()
    return _row_to_song(row) if row else None


async def increment_like(song_id: str, delta: int = 1) -> int:
    async with aiosqlite.connect(_DB) as db:
        await db.execute("""
            UPDATE songs SET likes = MAX(0, likes + ?) WHERE id = ?
        """, (delta, song_id))
        await db.commit()
        async with db.execute(
            "SELECT likes FROM songs WHERE id = ?", (song_id,)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else 0


async def delete_song(song_id: str):
    """Remove a song from the DB entirely (does not touch the file on disk)."""
    async with aiosqlite.connect(_DB) as db:
        await db.execute("DELETE FROM songs WHERE id = ?", (song_id,))
        await db.commit()


async def convert_song_in_db(
    old_id: str,
    new_id: str,
    file_path: str,
    duration: Optional[float],
    clear_cdg_path: bool = False,
) -> Optional[dict]:
    """Update DB after a successful transcode to MP4."""
    async with aiosqlite.connect(_DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id FROM songs WHERE id = ? AND id != ?", (new_id, old_id)
        ) as cur:
            collision = await cur.fetchone()
        if collision:
            await db.execute("DELETE FROM songs WHERE id = ?", (old_id,))
        else:
            cdg_col = ", cdg_path = NULL" if clear_cdg_path else ""
            await db.execute(
                f"UPDATE songs"
                f"   SET id = ?, file_path = ?, kind = 'video', duration_secs = ?{cdg_col}"
                f" WHERE id = ?",
                (new_id, file_path, duration, old_id),
            )
        await db.commit()
        async with db.execute("SELECT * FROM songs WHERE id = ?", (new_id,)) as cur:
            row = await cur.fetchone()
    return _row_to_song(row) if row else None


async def apply_mb_to_db(
    old_id: str,
    new_id: str,
    file_path: str,
    cdg_path: Optional[str],
    title: str,
    artist: str,
    year: Optional[int],
    genre: str,
) -> Optional[dict]:
    """
    Apply a MusicBrainz match to the database after files have been renamed.
    Handles the case where new_id already exists (e.g. duplicate path collision).
    Returns the updated song row. (Explicit user action; lock check is
    enforced at the caller before files are renamed on disk.)
    """
    existing = await get_song(old_id)
    if existing and existing.get("metadata_locked"):
        log.info(
            "apply_mb_to_db: overwriting locked row %s (%r by %r → %r by %r)",
            old_id, existing.get("title"), existing.get("artist"), title, artist,
        )

    async with aiosqlite.connect(_DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id FROM songs WHERE id = ? AND id != ?", (new_id, old_id)
        ) as cur:
            collision = await cur.fetchone()
        if collision:
            # New path collides with an existing different song — just drop the old row
            await db.execute("DELETE FROM songs WHERE id = ?", (old_id,))
        else:
            await db.execute("""
                UPDATE songs
                   SET id              = ?,
                       file_path       = ?,
                       cdg_path        = ?,
                       title           = ?,
                       artist          = ?,
                       year            = ?,
                       genre           = ?,
                       metadata_locked  = 1,
                       metadata_updated = 1
                 WHERE id = ?
            """, (new_id, file_path, cdg_path, title, artist, year, genre or "", old_id))
        await db.commit()
        async with db.execute("SELECT * FROM songs WHERE id = ?", (new_id,)) as cur:
            row = await cur.fetchone()
    return _row_to_song(row) if row else None


async def remove_missing_songs(valid_ids: set[str]) -> int:
    """
    Remove songs from DB whose files no longer exist on disk. Logs each
    locked row that is dropped so the owner can see what the scan removed.
    Returns the number of rows deleted.
    """
    if not valid_ids:
        return 0
    placeholders = ",".join("?" * len(valid_ids))
    async with aiosqlite.connect(_DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT id, title, artist, metadata_locked "
            f"FROM songs WHERE id NOT IN ({placeholders})",
            list(valid_ids),
        ) as cur:
            dead = await cur.fetchall()
        locked_dead = [r for r in dead if r["metadata_locked"]]
        for r in locked_dead:
            log.warning(
                "Scan removed locked row %s (%r by %r) — file is gone from disk.",
                r["id"], r["title"], r["artist"],
            )
        await db.execute(
            f"DELETE FROM songs WHERE id NOT IN ({placeholders})",
            list(valid_ids),
        )
        await db.commit()
    return len(dead)


# ── Legacy like count (kept for backwards compat with song router) ─────────────

async def get_like_count(song_id: str) -> int:
    song = await get_song(song_id)
    return song["likes"] if song else 0


async def get_like_counts() -> dict[str, int]:
    async with aiosqlite.connect(_DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id, likes FROM songs") as cur:
            rows = await cur.fetchall()
    return {r["id"]: r["likes"] for r in rows}
