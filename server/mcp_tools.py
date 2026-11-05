"""
MCP tool definitions and JSON-RPC 2.0 dispatcher for library cleanup.

Transport: JSON-RPC over HTTP (the "Streamable HTTP" transport without
the SSE channel — which is fine for pure request/response tools like
these).

Methods implemented:
  - initialize
  - notifications/initialized
  - tools/list
  - tools/call
"""
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable, Optional

import aiosqlite

from . import database as db
from .library import library
from .queue_manager import queue_manager
from .config import settings
from .auth import list_users as _list_registered_users

log = logging.getLogger(__name__)


# ── Tool implementations ─────────────────────────────────────────────────────

async def _t_library_stats() -> dict:
    stats = await db.get_library_stats()
    stats["total_tracked"] = await db.count_songs()
    return stats


async def _t_search_songs(
    query: str = "",
    sort: str = "title",
    kind: str = "",
    limit: int = 50,
    offset: int = 0,
    include_duplicates: bool = True,
) -> dict:
    limit = max(1, min(500, int(limit)))
    offset = max(0, int(offset))
    songs, total = await db.search_songs(
        query=query or "",
        sort=sort or "title",
        limit=limit,
        offset=offset,
        kind_filter=kind if kind in ("cdg", "video") else "",
        include_duplicates=bool(include_duplicates),
    )
    # Strip filesystem paths from the payload by default — they're noisy
    # and often contain the full server path. Agents can still get them
    # via get_song.
    trimmed = [
        {k: v for k, v in s.items() if k not in ("path", "cdg_path")}
        for s in songs
    ]
    return {"songs": trimmed, "total": total, "limit": limit, "offset": offset}


async def _t_get_song(song_id: str) -> dict:
    song = await db.get_song(song_id)
    if not song:
        raise ValueError(f"Song not found: {song_id}")
    return song


async def _t_update_song_metadata(
    song_id: str,
    title: Optional[str] = None,
    artist: Optional[str] = None,
    genre: Optional[str] = None,
    year: Optional[int] = None,
) -> dict:
    fields: dict = {}
    if title is not None:  fields["title"]  = title.strip()
    if artist is not None: fields["artist"] = artist.strip()
    if genre is not None:  fields["genre"]  = genre.strip()
    if year is not None:
        try: fields["year"] = int(year)
        except (TypeError, ValueError):
            raise ValueError("year must be an integer")
    if not fields:
        raise ValueError("No fields to update")
    updated = await db.update_song_metadata(song_id, fields)
    if not updated:
        raise ValueError(f"Song not found: {song_id}")
    return {"updated": updated, "fields": fields}


async def _t_delete_song(song_id: str, delete_file: bool = False) -> dict:
    """
    Delete a song row. When delete_file=True, also unlink the underlying
    media file(s) on disk (the MP3 and, for CDG entries, the .cdg).
    """
    existing = await db.get_song(song_id)
    if not existing:
        raise ValueError(f"Song not found: {song_id}")

    deleted_files: list[str] = []
    file_errors: list[str] = []
    if delete_file:
        for p in (existing.get("path"), existing.get("cdg_path")):
            if not p:
                continue
            try:
                if os.path.exists(p):
                    os.remove(p)
                    deleted_files.append(p)
            except OSError as e:
                file_errors.append(f"{p}: {e}")

    await db.delete_song(song_id)
    return {
        "deleted": True,
        "song_id": song_id,
        "title":   existing.get("title"),
        "artist":  existing.get("artist"),
        "deleted_files": deleted_files,
        "file_errors":   file_errors,
    }


async def _t_list_songs(
    limit: int = 100, offset: int = 0, sort: str = "title",
) -> dict:
    """
    Paginated dump of the entire library, sorted. Use for sweeping scans
    when you want to see everything (no text filter); search_songs is
    better when you have keywords.
    """
    limit = max(1, min(500, int(limit)))
    offset = max(0, int(offset))
    songs, total = await db.search_songs(
        query="",
        sort=sort or "title",
        limit=limit,
        offset=offset,
        kind_filter="",
        include_duplicates=True,
    )
    trimmed = [
        {k: v for k, v in s.items() if k not in ("path", "cdg_path")}
        for s in songs
    ]
    return {
        "songs": trimmed,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": (offset + limit) < total,
    }


# ── Queue tools ──────────────────────────────────────────────────────────────

async def _t_show_queue() -> dict:
    return {
        "queue":       queue_manager.get_queue(),
        "now_playing": queue_manager.now_playing(),
    }


async def _t_add_to_queue(
    song_id: str, user: str = "agent", play_next: bool = False,
) -> dict:
    song = await db.get_song(song_id)
    if not song:
        raise ValueError(f"Song not found: {song_id}")
    user = (user or "agent").strip() or "agent"
    if play_next:
        entry = await queue_manager.enqueue_next(song, user)
    else:
        entry = await queue_manager.enqueue(song, user)
    return {
        "added":     True,
        "queue_id":  entry.id,
        "song_id":   song["id"],
        "title":     song["title"],
        "artist":    song["artist"],
        "user":      user,
        "play_next": bool(play_next),
    }


async def _t_remove_from_queue(queue_id: str) -> dict:
    ok = await queue_manager.remove(queue_id)
    if not ok:
        raise ValueError(f"Queue entry not found: {queue_id}")
    return {"removed": True, "queue_id": queue_id}


async def _t_clear_queue() -> dict:
    """Remove every pending entry. The currently playing song is left alone."""
    n = await queue_manager.clear()
    return {"cleared": n}


async def _t_show_users() -> dict:
    """List registered users (from credentials.json) plus whoever has
    songs currently queued (which may include ad-hoc local-network
    guests who never logged in)."""
    registered = _list_registered_users(settings.db_path)
    in_queue = sorted({e["user"] for e in queue_manager.get_queue() if e.get("user")})
    now = queue_manager.now_playing()
    now_user = now.get("user") if now else None
    return {
        "registered_users":        registered,
        "users_with_queued_songs": in_queue,
        "now_playing_user":        now_user,
    }


async def _t_show_songs_queued_for_user(user: str) -> dict:
    user = (user or "").strip()
    if not user:
        raise ValueError("user is required")
    entries = [e for e in queue_manager.get_queue() if e.get("user") == user]
    now = queue_manager.now_playing()
    is_now = bool(now and now.get("user") == user)
    return {
        "user":           user,
        "entries":        entries,
        "count":          len(entries),
        "now_playing":    is_now,
        "now_playing_id": (now.get("queue_id") if is_now else None),
    }


async def _t_delete_songs_queued_for_user(user: str) -> dict:
    user = (user or "").strip()
    if not user:
        raise ValueError("user is required")
    removed = await queue_manager.remove_user_entries(user)
    return {"user": user, "removed": removed}


async def _t_find_duplicate_groups(limit: int = 50) -> dict:
    """
    Return groups of songs that share the same normalised (title, artist).
    Groups are ordered by size descending so the worst offenders come first.
    """
    import aiosqlite
    import re as _re

    limit = max(1, min(500, int(limit)))
    norm_sql = """
        SELECT LOWER(TRIM(title)) AS t, LOWER(TRIM(artist)) AS a, COUNT(*) AS n,
               GROUP_CONCAT(id) AS ids
        FROM songs
        WHERE title != ''
        GROUP BY t, a
        HAVING n > 1
        ORDER BY n DESC, t ASC
        LIMIT ?
    """
    groups: list[dict] = []
    async with aiosqlite.connect(db._DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(norm_sql, (limit,)) as cur:
            rows = await cur.fetchall()
        for r in rows:
            ids = (r["ids"] or "").split(",")
            # Fetch full rows for each member so the agent can pick which
            # one to keep (likes, kind, path all matter).
            members = []
            for sid in ids:
                s = await db.get_song(sid)
                if s:
                    members.append(s)
            groups.append({
                "title":   r["t"],
                "artist":  r["a"],
                "count":   r["n"],
                "song_ids": ids,
                "members": members,
            })
    return {"groups": groups, "returned": len(groups)}


async def _t_rescan_library() -> dict:
    """Start a filesystem rescan in the background (doesn't block)."""
    import asyncio
    asyncio.create_task(library.scan())
    return {"started": True}


# ── Database export / import ────────────────────────────────────────────────

_EXPORT_VERSION = 1
_EXPORT_FIELDS = (
    "id", "file_path", "cdg_path", "kind",
    "title", "artist", "year", "genre",
    "likes", "metadata_locked", "is_duplicate", "metadata_updated",
    "duration_secs", "added_at", "scanned_at",
)
_IMPORT_FIELDS = {
    "title", "artist", "year", "genre",
    "likes", "metadata_locked", "is_duplicate", "metadata_updated",
}


async def _t_export_db() -> dict:
    """
    Dump every row of the songs table. Use this as a backup before a
    destructive change, or to feed import_db later. The payload includes
    file paths so an import on a differently-rooted library can still
    match by id (the id itself is sha256(path)).
    """
    async with aiosqlite.connect(db._DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            f"SELECT {', '.join(_EXPORT_FIELDS)} FROM songs ORDER BY id"
        ) as cur:
            rows = await cur.fetchall()

    songs = [dict(r) for r in rows]
    return {
        "version":     _EXPORT_VERSION,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count":       len(songs),
        "songs":       songs,
    }


async def _t_import_db(
    songs: list,
    match: str = "id",
    dry_run: bool = False,
) -> dict:
    """
    Merge metadata from an export_db payload back into the current songs
    table. Rows are matched by `match` ('id' or 'file_path'); unmatched
    rows are reported and left untouched (no inserts). Only metadata
    fields are written — file_path, cdg_path, kind, added_at and
    scanned_at are never overwritten.
    """
    if not isinstance(songs, list):
        raise ValueError("`songs` must be a list of row dicts from export_db")
    if match not in ("id", "file_path"):
        raise ValueError("`match` must be 'id' or 'file_path'")

    updated = 0
    skipped: list[dict] = []
    updates: list[tuple] = []

    async with aiosqlite.connect(db._DB) as conn:
        conn.row_factory = aiosqlite.Row
        if match == "id":
            async with conn.execute("SELECT id FROM songs") as cur:
                existing = {r[0] for r in await cur.fetchall()}
            key_lookup = {sid: sid for sid in existing}
        else:
            async with conn.execute("SELECT id, file_path FROM songs") as cur:
                rows = await cur.fetchall()
            key_lookup = {r["file_path"]: r["id"] for r in rows}

        for src in songs:
            if not isinstance(src, dict):
                skipped.append({"reason": "not-a-dict", "value": src})
                continue
            key = src.get(match)
            if key is None:
                skipped.append({"reason": f"missing-{match}", "src": src})
                continue
            sid = key_lookup.get(key)
            if sid is None:
                skipped.append({
                    "reason":  "no-match",
                    "match":   match,
                    "key":     key,
                    "title":   src.get("title"),
                    "artist":  src.get("artist"),
                })
                continue

            fields = {k: src[k] for k in _IMPORT_FIELDS if k in src}
            if not fields:
                skipped.append({"reason": "no-fields", "id": sid})
                continue
            set_clause = ", ".join(f"{k} = ?" for k in fields)
            updates.append((set_clause, list(fields.values()) + [sid]))

        if not dry_run:
            for set_clause, values in updates:
                await conn.execute(
                    f"UPDATE songs SET {set_clause} WHERE id = ?", values
                )
            if updates:
                await conn.commit()
            updated = len(updates)

    return {
        "matched":       len(updates),
        "updated":       updated,
        "skipped_count": len(skipped),
        "skipped":       skipped[:50],   # cap report size
        "dry_run":       bool(dry_run),
        "match":         match,
    }


# ── Tool registry ────────────────────────────────────────────────────────────

ToolHandler = Callable[..., Awaitable[Any]]


TOOLS: dict[str, dict] = {
    "library_stats": {
        "description": (
            "Return totals and counts broken down by kind (CDG+MP3 vs video) "
            "and any duplicate-flagged entries."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": _t_library_stats,
    },
    "search_songs": {
        "description": (
            "Paginated search across title/artist/genre. Sort by title, artist, "
            "likes, year or genre. Filter by kind='cdg' or 'video'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query":  {"type": "string", "description": "Space-separated words; each must appear in title/artist/genre."},
                "sort":   {"type": "string", "enum": ["title", "artist", "likes", "year", "genre"]},
                "kind":   {"type": "string", "enum": ["", "cdg", "video"]},
                "limit":  {"type": "integer", "minimum": 1, "maximum": 500},
                "offset": {"type": "integer", "minimum": 0},
                "include_duplicates": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        "handler": _t_search_songs,
    },
    "get_song": {
        "description": "Return the full row for one song by id (includes path).",
        "inputSchema": {
            "type": "object",
            "properties": {"song_id": {"type": "string"}},
            "required": ["song_id"],
            "additionalProperties": False,
        },
        "handler": _t_get_song,
    },
    "update_song_metadata": {
        "description": (
            "Rewrite one or more metadata fields on a song. Any omitted field "
            "is left as-is. Returns the updated row."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "song_id": {"type": "string"},
                "title":   {"type": "string"},
                "artist":  {"type": "string"},
                "genre":   {"type": "string"},
                "year":    {"type": "integer"},
            },
            "required": ["song_id"],
            "additionalProperties": False,
        },
        "handler": _t_update_song_metadata,
    },
    "delete_song": {
        "description": (
            "Destructive: remove a song row from the database. By default the "
            "underlying media file is left on disk (and would be re-added by "
            "the next rescan). Pass delete_file=true to also unlink the "
            "media file(s) — the .mp3/.cdg pair for CDG entries, the video "
            "file for video entries. Returns which paths were deleted."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "song_id":     {"type": "string"},
                "delete_file": {"type": "boolean", "default": False,
                                "description": "Also unlink the media file(s) on disk."},
            },
            "required": ["song_id"],
            "additionalProperties": False,
        },
        "handler": _t_delete_song,
    },
    "list_songs": {
        "description": (
            "Paginated dump of the full library (no text filter). Use offset "
            "+ has_more to iterate. For keyword search, use search_songs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit":  {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "sort":   {"type": "string", "enum": ["title", "artist", "likes", "year", "genre"], "default": "title"},
            },
            "additionalProperties": False,
        },
        "handler": _t_list_songs,
    },
    "show_queue": {
        "description": "Return the current play queue and the now-playing entry.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": _t_show_queue,
    },
    "add_to_queue": {
        "description": (
            "Add a song to the play queue. Set play_next=true to insert at "
            "the front (plays after the current song)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "song_id":   {"type": "string"},
                "user":      {"type": "string", "description": "Name shown as the requester.", "default": "agent"},
                "play_next": {"type": "boolean", "default": False},
            },
            "required": ["song_id"],
            "additionalProperties": False,
        },
        "handler": _t_add_to_queue,
    },
    "remove_from_queue": {
        "description": "Remove one pending entry by its queue_id (NOT the song_id).",
        "inputSchema": {
            "type": "object",
            "properties": {"queue_id": {"type": "string"}},
            "required": ["queue_id"],
            "additionalProperties": False,
        },
        "handler": _t_remove_from_queue,
    },
    "clear_queue": {
        "description": (
            "Destructive: remove every pending entry. The currently playing "
            "song is left alone — use a separate skip call if you want to "
            "also stop it."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": _t_clear_queue,
    },
    "show_users": {
        "description": (
            "Return registered usernames from credentials.json plus anyone "
            "with songs currently in the queue (ad-hoc local guests included)."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": _t_show_users,
    },
    "show_songs_queued_for_user": {
        "description": "List all pending queue entries whose requester matches the given user.",
        "inputSchema": {
            "type": "object",
            "properties": {"user": {"type": "string"}},
            "required": ["user"],
            "additionalProperties": False,
        },
        "handler": _t_show_songs_queued_for_user,
    },
    "delete_songs_queued_for_user": {
        "description": (
            "Destructive: remove every pending queue entry for the given "
            "user. The currently playing song is left alone."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"user": {"type": "string"}},
            "required": ["user"],
            "additionalProperties": False,
        },
        "handler": _t_delete_songs_queued_for_user,
    },
    "find_duplicate_groups": {
        "description": (
            "Return groups of songs sharing the same normalised (title, artist). "
            "Use this to spot duplicates before cleaning them up."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 500}},
            "additionalProperties": False,
        },
        "handler": _t_find_duplicate_groups,
    },
    "rescan_library": {
        "description": (
            "Start a background filesystem rescan. Returns immediately; the "
            "scan completes asynchronously and new songs will show up after."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": _t_rescan_library,
    },
    "export_db": {
        "description": (
            "Dump every row of the songs table as JSON (full backup). "
            "Pair with import_db to restore metadata after a rescan or a "
            "destructive cleanup."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": _t_export_db,
    },
    "import_db": {
        "description": (
            "Destructive: merge metadata from an export_db payload back "
            "into the songs table. Only metadata fields (title/artist/"
            "year/genre/likes/metadata_locked/is_duplicate/metadata_updated) "
            "are written; file paths and kinds are never overwritten. "
            "Rows are matched by `match`='id' (default) or 'file_path'; "
            "unmatched rows are reported in `skipped` and left untouched "
            "(no inserts). Pass dry_run=true to preview without writing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "songs":   {"type": "array",
                            "description": "The `songs` array from an export_db response."},
                "match":   {"type": "string", "enum": ["id", "file_path"], "default": "id"},
                "dry_run": {"type": "boolean", "default": False},
            },
            "required": ["songs"],
            "additionalProperties": False,
        },
        "handler": _t_import_db,
    },
}


# ── JSON-RPC dispatcher ──────────────────────────────────────────────────────

_PROTOCOL_VERSION = "2024-11-05"
_SERVER_INFO = {"name": "superkaraoke", "version": "1.0.0"}


def _rpc_error(req_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _rpc_result(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


async def dispatch(req: dict) -> Optional[dict]:
    """
    Dispatch a single JSON-RPC request. Returns the response dict, or
    None for notifications (requests with no id).
    """
    req_id = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}
    is_notification = "id" not in req

    try:
        if method == "initialize":
            return _rpc_result(req_id, {
                "protocolVersion": _PROTOCOL_VERSION,
                "serverInfo": _SERVER_INFO,
                "capabilities": {"tools": {"listChanged": False}},
            })

        if method == "notifications/initialized":
            return None  # notification, no response expected

        if method == "tools/list":
            return _rpc_result(req_id, {
                "tools": [
                    {
                        "name": name,
                        "description": t["description"],
                        "inputSchema": t["inputSchema"],
                    }
                    for name, t in TOOLS.items()
                ],
            })

        if method == "tools/call":
            tool_name = params.get("name")
            args = params.get("arguments") or {}
            tool = TOOLS.get(tool_name)
            if not tool:
                return _rpc_error(req_id, -32602, f"Unknown tool: {tool_name}")
            try:
                data = await tool["handler"](**args)
                text = json.dumps(data, default=str, indent=2, sort_keys=False)
                return _rpc_result(req_id, {
                    "content": [{"type": "text", "text": text}],
                    "isError": False,
                })
            except TypeError as e:
                return _rpc_error(req_id, -32602, f"Invalid arguments: {e}")
            except Exception as e:
                log.exception("[mcp] tool %s failed", tool_name)
                return _rpc_result(req_id, {
                    "content": [{"type": "text", "text": f"Error: {e}"}],
                    "isError": True,
                })

        if is_notification:
            return None
        return _rpc_error(req_id, -32601, f"Method not found: {method}")

    except Exception as e:
        log.exception("[mcp] dispatch error")
        if is_notification:
            return None
        return _rpc_error(req_id, -32603, f"Internal error: {e}")
