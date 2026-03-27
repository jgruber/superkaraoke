"""
Library management endpoints.

GET  /api/library              — paginated song list with all metadata fields
PATCH /api/library/{song_id}   — update editable metadata (title, artist, year, genre, likes)
POST /api/library/{song_id}/redetect — re-run auto-detection from file tags/filename
POST /api/library/{song_id}/lookup  — MusicBrainz search (returns candidates)
POST /api/library/scan         — trigger a background filesystem rescan
GET  /api/library/stats        — aggregate counts for dashboard header
"""
import hashlib
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional

from ..library import library
from ..config import settings
from ..database import (
    get_song, update_song_metadata, redetect_song_metadata,
    get_library_stats, search_songs, delete_song, init_db, count_songs,
    apply_mb_to_db, convert_song_in_db,
)
from ..metadata import extract_metadata, search_musicbrainz, strip_style_of, _TRAIL_YTID

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

    # Strip YouTube video ID suffix (e.g. "_dQw4w9WgXcQ" or " [dQw4w9WgXcQ]")
    search_title = _TRAIL_YTID.sub('', search_title).strip()

    # Strip "in the style of Artist" convention.  The embedded attribution
    # always overrides whatever artist we had before (e.g. a directory-derived
    # artist like "Sing King" should not beat an explicit "Icona Pop").
    clean_title, style_artist = strip_style_of(search_title)
    search_title = clean_title
    if style_artist:
        search_artist = style_artist

    results = await search_musicbrainz(search_title, search_artist)
    return {"results": results, "query": {"title": search_title, "artist": search_artist}}


# ── Convert to MP4 ─────────────────────────────────────────────────────────────

@router.post("/library/{song_id}/convert")
async def convert_to_mp4(song_id: str):
    """
    Transcode a CDG+MP3 pair or non-MP4 video to a browser-native H.264/AAC
    faststart MP4 using the same ffmpeg settings as convert_media.py.

    Runs synchronously in a thread pool — the request blocks until ffmpeg
    finishes so the caller always gets the final updated song back.
    """
    import asyncio
    from library_scripts.convert_media import transcode_video, transcode_cdg, probe_duration

    song = await get_song(song_id)
    if not song:
        raise HTTPException(status_code=404, detail="Song not found")

    fp  = Path(song["file_path"])
    cp  = Path(song["cdg_path"]) if song.get("cdg_path") else None
    mp4 = fp.with_suffix(".mp4")

    # Already an MP4 — nothing to do
    if fp.suffix.lower() == ".mp4" and song["kind"] == "video":
        return song

    if not fp.exists():
        raise HTTPException(status_code=400, detail=f"Source file not found: {fp}")
    if cp and not cp.exists():
        raise HTTPException(status_code=400, detail=f"CDG file not found: {cp}")

    media_dir = settings.media_dir
    try:
        rel = str(mp4.relative_to(media_dir))
    except ValueError:
        raise HTTPException(status_code=400, detail="Song path is outside media directory")
    new_id = hashlib.sha256(rel.encode()).hexdigest()[:12]

    # Run ffmpeg in a thread (can take minutes for CDG or large video)
    if song["kind"] == "cdg":
        ok = await asyncio.to_thread(transcode_cdg, fp, cp, mp4)
    else:
        ok = await asyncio.to_thread(transcode_video, fp, mp4)

    if not ok:
        raise HTTPException(status_code=500, detail="ffmpeg transcoding failed — check server logs")

    duration = await asyncio.to_thread(probe_duration, mp4)

    # Remove source files
    for f in ([fp, cp] if cp else [fp]):
        try:
            f.unlink()
        except OSError as exc:
            log.warning("Could not remove source file %s: %s", f, exc)

    updated = await convert_song_in_db(
        old_id=song_id,
        new_id=new_id,
        file_path=str(mp4),
        duration=duration,
        clear_cdg_path=(cp is not None),
    )
    if not updated:
        raise HTTPException(status_code=500, detail="Database update failed after transcode")
    return updated


# ── MusicBrainz apply (rename + full DB update) ────────────────────────────────

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class MbApplyBody(BaseModel):
    title:  str
    artist: str
    year:   Optional[int] = None
    genre:  Optional[str] = None


@router.post("/library/{song_id}/mb-apply")
async def mb_apply(song_id: str, body: MbApplyBody):
    """
    Apply a MusicBrainz result to a song: rename the file(s) on disk to
    'Artist - Title.ext', recompute the song ID, and update all metadata.
    """
    song = await get_song(song_id)
    if not song:
        raise HTTPException(status_code=404, detail="Song not found")

    fp  = Path(song["file_path"])
    cp  = Path(song["cdg_path"]) if song.get("cdg_path") else None
    ext = fp.suffix.lower()

    safe_stem = _UNSAFE_CHARS.sub("", f"{body.artist} - {body.title}").strip().rstrip(".")
    new_fp = fp.parent / f"{safe_stem}{ext}"
    new_cp = (cp.parent / f"{safe_stem}.cdg") if cp else None

    media_dir = settings.media_dir
    try:
        rel = str(new_fp.relative_to(media_dir))
    except ValueError:
        raise HTTPException(status_code=400, detail="New path is outside media directory")
    new_id = hashlib.sha256(rel.encode()).hexdigest()[:12]

    # Rename audio file
    if fp != new_fp:
        if not fp.exists():
            raise HTTPException(status_code=400, detail=f"Source file not found: {fp}")
        try:
            fp.rename(new_fp)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Rename failed: {exc}")

    # Rename CDG file (non-fatal if it fails)
    if cp and new_cp and cp != new_cp and cp.exists():
        try:
            cp.rename(new_cp)
        except OSError:
            new_cp = cp  # keep old CDG path if rename failed

    updated = await apply_mb_to_db(
        old_id=song_id,
        new_id=new_id,
        file_path=str(new_fp),
        cdg_path=str(new_cp) if new_cp else None,
        title=body.title,
        artist=body.artist,
        year=body.year,
        genre=body.genre or "",
    )
    if not updated:
        raise HTTPException(status_code=500, detail="Database update failed")
    return updated


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
