"""
Library management endpoints.

GET  /api/library              — paginated song list with all metadata fields
PATCH /api/library/{song_id}   — update editable metadata (title, artist, year, genre, likes)
POST /api/library/{song_id}/redetect — re-run auto-detection from file tags/filename
POST /api/library/{song_id}/lookup  — MusicBrainz search (returns candidates)
POST /api/library/scan         — trigger a background filesystem rescan
GET  /api/library/stats        — aggregate counts for dashboard header
"""
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional

from ..library import library
from ..config import settings
from ..database import (
    get_song, update_song_metadata, redetect_song_metadata,
    get_library_stats, search_songs, delete_song, init_db, count_songs,
)
from ..metadata import extract_metadata, search_musicbrainz

router = APIRouter(tags=["library"])


# ── List / search ──────────────────────────────────────────────────────────────

@router.get("/library")
async def list_library(
    q: str = Query(""),
    sort: str = Query("title"),
    kind: str = Query("", description="Filter: cdg | video | (empty = all)"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    songs, total = await search_songs(
        query=q, sort=sort, kind_filter=kind, limit=limit, offset=offset
    )
    return {"songs": songs, "total": total, "offset": offset, "limit": limit}


@router.get("/library/stats")
async def library_stats():
    return await get_library_stats()


# ── Metadata edit ──────────────────────────────────────────────────────────────

class MetadataUpdate(BaseModel):
    title:  Optional[str] = None
    artist: Optional[str] = None
    year:   Optional[int] = None
    genre:  Optional[str] = None
    likes:  Optional[int] = None
    metadata_locked: Optional[int] = None
    is_duplicate:    Optional[int] = None


@router.patch("/library/{song_id}")
async def update_metadata(song_id: str, body: MetadataUpdate):
    if not await get_song(song_id):
        raise HTTPException(status_code=404, detail="Song not found")

    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    updated = await update_song_metadata(song_id, fields)
    return updated


@router.delete("/library/{song_id}")
async def remove_song(song_id: str):
    if not await get_song(song_id):
        raise HTTPException(status_code=404, detail="Song not found")
    await delete_song(song_id)
    return {"deleted": song_id}


# ── Re-detect from file ────────────────────────────────────────────────────────

@router.post("/library/{song_id}/redetect")
async def redetect_metadata(song_id: str):
    song = await get_song(song_id)
    if not song:
        raise HTTPException(status_code=404, detail="Song not found")

    import asyncio
    meta = await asyncio.to_thread(
        extract_metadata,
        song["file_path"],
        song.get("cdg_path"),
        song["kind"],
    )
    updated = await redetect_song_metadata(song_id, meta)
    return updated


# ── MusicBrainz lookup ─────────────────────────────────────────────────────────

@router.post("/library/{song_id}/lookup")
async def musicbrainz_lookup(
    song_id: str,
    title: str = Query(""),
    artist: str = Query(""),
):
    song = await get_song(song_id)
    if not song:
        raise HTTPException(status_code=404, detail="Song not found")

    # Fall back to stored values if not provided
    search_title  = title.strip()  or song.get("title",  "")
    search_artist = artist.strip() or song.get("artist", "")

    results = await search_musicbrainz(search_title, search_artist)
    return {"results": results, "query": {"title": search_title, "artist": search_artist}}


# ── Rescan ─────────────────────────────────────────────────────────────────────

@router.post("/library/scan")
async def trigger_scan(background_tasks: BackgroundTasks):
    background_tasks.add_task(library.scan)
    return {"status": "scan started"}


# ── Export / Import ────────────────────────────────────────────────────────────

_SQLITE_MAGIC = b"SQLite format 3\x00"


@router.get("/library/export")
async def export_database():
    if not settings.db_path.exists():
        raise HTTPException(status_code=404, detail="Database not found")
    return FileResponse(
        str(settings.db_path),
        media_type="application/x-sqlite3",
        filename="superkaraoke.db",
    )


@router.post("/library/import")
async def import_database(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
):
    content = await file.read()

    if len(content) < 16 or content[:16] != _SQLITE_MAGIC:
        raise HTTPException(status_code=400, detail="Not a valid SQLite database file")

    tmp_path = settings.db_path.with_suffix(".import_tmp")
    try:
        tmp_path.write_bytes(content)
        tmp_path.rename(settings.db_path)
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Failed to save database: {exc}")

    # Apply any pending schema migrations to the imported DB
    await init_db()
    library._song_count = await count_songs()

    # Rescan in background to validate file paths against this server's media dir
    background_tasks.add_task(library.scan)

    return {"status": "imported", "songs": library._song_count}
