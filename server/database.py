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
                    (id, file_path, cdg_path, kind, title, artist, year, genre, likes, scanned_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
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
                (id, file_path, cdg_path, kind, title, artist, year, genre, likes, scanned_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, datetime('now'))
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
    q = f"%{query}%" if query else "%"

    conditions = ["(title LIKE ? OR artist LIKE ? OR genre LIKE ?)"]
    base_params: list = [q, q, q]

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


async def update_song_metadata(song_id: str, fields: dict) -> Optional[dict]:
    """
    Update user-editable metadata fields.
    Automatically sets metadata_locked = 1 to protect against rescan overwrites.
    """
    allowed = {"title", "artist", "year", "genre", "likes", "metadata_locked", "is_duplicate"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return await get_song(song_id)

    # Always lock when user explicitly saves
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


async def redetect_song_metadata(song_id: str, metadata: dict) -> Optional[dict]:
    """Apply freshly-detected metadata and clear the lock."""
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


async def remove_missing_songs(valid_ids: set[str]):
    """Remove songs from DB whose files no longer exist on disk."""
    if not valid_ids:
        return
    placeholders = ",".join("?" * len(valid_ids))
    async with aiosqlite.connect(_DB) as db:
        await db.execute(
            f"DELETE FROM songs WHERE id NOT IN ({placeholders})",
            list(valid_ids),
        )
        await db.commit()


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
