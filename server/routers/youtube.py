"""
YouTube search and download via yt-dlp.

GET  /api/youtube/search?q=query   — search YouTube, returns up to 15 results
POST /api/youtube/download          — start a download job, returns {job_id}
GET  /api/youtube/download/{job_id} — poll job status
"""
import asyncio
import logging
import uuid
from collections import OrderedDict
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel

from ..config import settings

log = logging.getLogger(__name__)
router = APIRouter(tags=["youtube"])

# In-memory job store — capped at 100 entries (oldest dropped)
_jobs: OrderedDict[str, dict] = OrderedDict()
_MAX_JOBS = 100

DOWNLOADS_DIR = settings.media_dir / "Downloads"


# ── Search ──────────────────────────────────────────────────────────────────────

@router.get("/youtube/search")
async def youtube_search(q: str = Query(..., min_length=1)):
    query = f"{q.strip()} karaoke"
    loop = asyncio.get_running_loop()
    results = await loop.run_in_executor(None, _do_search, query)
    return {"results": results, "query": query}


def _do_search(query: str) -> list[dict]:
    try:
        from yt_dlp import YoutubeDL
        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": "in_playlist",
        }
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch15:{query}", download=False)
            entries = (info or {}).get("entries") or []
            results = []
            for e in entries:
                vid_id = e.get("id", "")
                if not vid_id:
                    continue
                duration_s = int(e.get("duration") or 0)
                duration = f"{duration_s // 60}:{duration_s % 60:02d}" if duration_s else ""
                results.append({
                    "id": vid_id,
                    "title": e.get("title", ""),
                    "channel": e.get("channel") or e.get("uploader", ""),
                    "duration": duration,
                    "thumbnail": f"https://i.ytimg.com/vi/{vid_id}/mqdefault.jpg",
                    "url": f"https://www.youtube.com/watch?v={vid_id}",
                })
            return results
    except Exception as exc:
        log.error("YouTube search failed: %s", exc)
        return []


# ── Download ────────────────────────────────────────────────────────────────────

class DownloadRequest(BaseModel):
    url: str
    title: str
    channel: str = ""


@router.post("/youtube/download")
async def start_download(body: DownloadRequest, background_tasks: BackgroundTasks):
    job_id = uuid.uuid4().hex[:8]

    # Evict oldest jobs if over cap
    while len(_jobs) >= _MAX_JOBS:
        _jobs.popitem(last=False)

    _jobs[job_id] = {
        "status": "pending",
        "progress": 0,
        "title": body.title,
        "channel": body.channel,
        "filename": None,
        "error": None,
    }
    background_tasks.add_task(_run_download, job_id, body.url, body.channel)
    return {"job_id": job_id}


@router.get("/youtube/download/{job_id}")
async def download_status(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


# ── Internal download logic ─────────────────────────────────────────────────────

def _make_progress_hook(job_id: str):
    """Update download percentage while bytes are transferring."""
    def hook(d: dict):
        job = _jobs.get(job_id)
        if job is None:
            return
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            job["progress"] = int(downloaded / total * 100) if total else 0
            job["status"] = "downloading"
        elif d["status"] == "finished":
            job["progress"] = 99   # ffmpeg merge may still be running
            job["status"] = "processing"
    return hook


def _make_postprocessor_hook(job_id: str):
    """Capture the final merged filename after ffmpeg is done."""
    def hook(d: dict):
        job = _jobs.get(job_id)
        if job is None:
            return
        if d.get("status") == "finished":
            info = d.get("info_dict", {})
            # filepath is the final output path after postprocessing
            final = (
                d.get("filepath")
                or info.get("filepath")
                or info.get("filename")
                or ""
            )
            if final:
                job["filename"] = str(final)
    return hook


async def _run_download(job_id: str, url: str, channel: str):
    job = _jobs[job_id]
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _do_download, job_id, url)
        job["status"] = "done"
        job["progress"] = 100

        # Rescan so the new file is indexed
        from ..library import library
        from ..library import _song_id
        from ..database import get_song, update_song_metadata
        await library.scan()

        # Resolve song_id from the known file path (reliable — no title search)
        filename = job.get("filename") or ""
        if filename:
            sid = _song_id(Path(filename))
            song = await get_song(sid)
            if song:
                job["song_id"] = sid
                # If the scan left artist blank, fill it from the YouTube channel
                if not song.get("artist") and channel:
                    await update_song_metadata(sid, {"artist": channel, "metadata_locked": 0})
                    log.info("Set artist='%s' for downloaded song %s", channel, sid)
            else:
                log.warning("Downloaded file not found in DB after scan: %s", filename)
        else:
            log.warning("No final filename recorded for job %s", job_id)

    except Exception as exc:
        log.error("Download failed for job %s: %s", job_id, exc)
        job["status"] = "error"
        job["error"] = str(exc)


def _do_download(job_id: str, url: str):
    from yt_dlp import YoutubeDL
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": str(DOWNLOADS_DIR / "%(title)s.%(ext)s"),
        "merge_output_format": "mp4",
        "progress_hooks": [_make_progress_hook(job_id)],
        "postprocessor_hooks": [_make_postprocessor_hook(job_id)],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    with YoutubeDL(opts) as ydl:
        ydl.download([url])
