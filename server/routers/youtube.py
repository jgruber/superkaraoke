"""
YouTube search and download via yt-dlp.

GET  /api/youtube/search?q=query   — search YouTube, returns up to 15 results
POST /api/youtube/download          — start a download job, returns {job_id}
GET  /api/youtube/download/{job_id} — poll job status
"""
import asyncio
import hashlib
import logging
import re
import uuid
from collections import OrderedDict
from pathlib import Path

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

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
        job["message"] = "Downloading from YouTube…"
        await loop.run_in_executor(None, _do_download, job_id, url)
        # Keep status as "processing" until song_id is written, so the frontend
        # poll doesn't hit done+no-song_id and bail before auto-queue.
        job["status"] = "processing"
        job["progress"] = 100
        job["message"] = "Indexing file…"

        from ..library import _song_id
        from ..database import (
            get_song, update_song_metadata, apply_mb_to_db,
            bulk_upsert_songs, touch_song_paths,
        )
        from ..metadata import (
            extract_metadata, parse_filename, search_musicbrainz,
            clean_for_mb_query, clean_youtube_title,
            split_title_candidates, pick_best_mb_match,
        )

        filename = job.get("filename") or ""
        if not filename:
            log.warning("No final filename recorded for job %s", job_id)
            job["status"] = "done"
            return

        fp  = Path(filename)
        sid = _song_id(fp)

        # Index the new file directly rather than waiting for a full scan.
        # Full scans can take a minute on big libraries and are skipped if
        # one is already in progress (e.g. startup scan) — leaving the
        # downloaded file unindexed and the queue-add silently dropped.
        meta = await loop.run_in_executor(
            None, extract_metadata, str(fp), None, "video", True,
        )

        # Pull in richer metadata from the yt-dlp info dict captured during
        # the download. YouTube Music videos expose track/artist/release_year
        # directly; regular uploads give uploader/upload_date/tags.
        yt_info = job.get("yt_info") or {}
        yt_title_raw = yt_info.get("title") or meta.get("title") or fp.stem
        yt_title = clean_youtube_title(yt_title_raw)
        yt_track = (yt_info.get("track") or "").strip()
        yt_artist = (yt_info.get("artist") or "").strip()
        yt_year   = yt_info.get("release_year")
        if not yt_year:
            upload = yt_info.get("upload_date") or ""
            if len(upload) >= 4 and upload[:4].isdigit():
                yt_year = int(upload[:4])

        # When yt-dlp didn't give us track/artist (plain upload), split the
        # cleaned title on " - " as karaoke titles conventionally do:
        # "Artist - Song Name". Without this we'd ship the whole string to
        # MB as the recording title and never match.
        split_artist, split_title = "", yt_title
        if not yt_track:
            parts = parse_filename(yt_title)
            if parts.get("artist") and parts.get("title"):
                split_artist = parts["artist"]
                split_title  = parts["title"]

        # Seed title/artist/year from the best signal available.
        meta_title  = yt_track or split_title or yt_title or meta.get("title") or fp.stem
        meta_artist = yt_artist or split_artist or meta.get("artist") or channel or yt_info.get("uploader", "")
        meta_year   = meta.get("year") or yt_year
        meta_genre  = meta.get("genre") or ""
        # Lock so the next library scan can't clobber what we just set.
        meta.update({
            "title":  meta_title,
            "artist": meta_artist,
            "year":   meta_year,
            "genre":  meta_genre,
        })

        await bulk_upsert_songs([{
            "id": sid, "file_path": str(fp), "cdg_path": None, "kind": "video",
            **meta,
        }])
        await touch_song_paths(sid, str(fp), None, "video")
        # Write the YT-derived fields on top (bulk_upsert is INSERT OR IGNORE,
        # so for an existing row we need this to overwrite stale values).
        await update_song_metadata(
            sid,
            {
                "title":           meta_title,
                "artist":          meta_artist,
                "year":            meta_year,
                "genre":           meta_genre,
                "metadata_locked": 0,
            },
            force=True,
        )

        # NOTE: we intentionally don't kick off library.scan() here. A
        # concurrent scan would race against the upcoming MB rename:
        # its remove_missing_songs() snapshot predates the id migration,
        # and the fresh row gets deleted as "missing on disk". Everything
        # scan would pick up (the one new file) we've already upserted
        # above, so there's nothing extra to reconcile.

        song = await get_song(sid)
        if not song:
            log.warning("Downloaded file not in DB after direct upsert: %s", filename)
            return

        # ── MusicBrainz auto-fix ──────────────────────────────────────────────
        # Generate (title, artist) candidate pairs from the cleaned YT title
        # (handles " - ", " | ", " – " separators and either ordering), then
        # hit MB until one matches. If yt-dlp gave us explicit track+artist,
        # that's the strongest candidate so we put it first.
        clean_title, style_artist = clean_for_mb_query(yt_title)
        fallback_artist = yt_artist or style_artist or meta_artist or ""

        candidate_pairs: list[tuple[str, str]] = []
        if yt_track:
            candidate_pairs.append((yt_track, fallback_artist))
        candidate_pairs.extend(split_title_candidates(clean_title, fallback_artist))

        mb_applied = False
        best = None
        if candidate_pairs:
            job["message"] = "Looking up MusicBrainz…"
            # Within match band, prefer oldest release year (original recording
            # over later covers / re-releases).
            for cand_title, cand_artist in candidate_pairs:
                try:
                    results = await search_musicbrainz(cand_title, cand_artist)
                    candidate = pick_best_mb_match(
                        results, min_score=90, prefer_oldest=True,
                    )
                    if candidate:
                        best = candidate
                        log.info(
                            "MB match: title=%r artist=%r → '%s - %s' year=%s score=%d",
                            cand_title, cand_artist,
                            candidate["artist"], candidate["title"],
                            candidate.get("year"), candidate.get("score", 0),
                        )
                        break
                except Exception as exc:
                    log.warning("MB query failed (%r / %r): %s",
                                cand_title, cand_artist, exc)
                    continue
            # Last-ditch: just the cleaned title alone
            if not best and clean_title:
                try:
                    results = await search_musicbrainz(clean_title)
                    best = pick_best_mb_match(
                        results, min_score=95, prefer_oldest=True,
                    )
                except Exception as exc:
                    log.warning("MB title-only fallback failed: %s", exc)

        if best:
            try:
                fp  = Path(song["file_path"])
                cp  = Path(song["cdg_path"]) if song.get("cdg_path") else None
                ext = fp.suffix.lower()

                safe_stem = _UNSAFE_CHARS.sub(
                    "", f"{best['artist']} - {best['title']}"
                ).strip().rstrip(".")
                new_fp = fp.parent / f"{safe_stem}{ext}"
                new_cp = (cp.parent / f"{safe_stem}.cdg") if cp else None

                rel    = str(new_fp.relative_to(settings.media_dir))
                new_id = hashlib.sha256(rel.encode()).hexdigest()[:12]

                if fp != new_fp and fp.exists():
                    fp.rename(new_fp)
                if cp and new_cp and cp != new_cp and cp.exists():
                    cp.rename(new_cp)

                updated = await apply_mb_to_db(
                    old_id   = sid,
                    new_id   = new_id,
                    file_path= str(new_fp),
                    cdg_path = str(new_cp) if new_cp else None,
                    title    = best["title"],
                    artist   = best["artist"],
                    year     = best.get("year") or yt_year,
                    genre    = best.get("genre") or "",
                )
                if updated:
                    sid = new_id
                    mb_applied = True
                    job["mb_applied"] = {
                        "title":  best["title"],
                        "artist": best["artist"],
                        "score":  best["score"],
                    }
                    log.info("MB auto-fix: '%s - %s' (score=%d)",
                             best["artist"], best["title"], best["score"])
            except Exception as exc:
                log.warning("MB apply failed for job %s: %s", job_id, exc)

        # ── Fallback: rename file to a clean "{artist} - {title}.ext" and
        #             fill year/genre from yt-dlp info when MB missed. ──────
        if not mb_applied:
            song = await get_song(sid)
            if song:
                cur_artist = song.get("artist") or ""
                cur_title  = song.get("title")  or ""
                cur_year   = song.get("year")
                cur_genre  = song.get("genre") or ""
                fallback_genre = _guess_genre_from_tags(yt_info.get("tags") or [])

                rename_stem = None
                if cur_artist and cur_title:
                    rename_stem = _UNSAFE_CHARS.sub(
                        "", f"{cur_artist} - {cur_title}"
                    ).strip().rstrip(".")
                elif cur_title:
                    rename_stem = _UNSAFE_CHARS.sub("", cur_title).strip().rstrip(".")

                fp = Path(song["file_path"])
                new_sid = sid
                if rename_stem and fp.stem != rename_stem:
                    new_fp = fp.parent / f"{rename_stem}{fp.suffix}"
                    if fp.exists() and not new_fp.exists():
                        try:
                            fp.rename(new_fp)
                            rel     = str(new_fp.relative_to(settings.media_dir))
                            new_sid = hashlib.sha256(rel.encode()).hexdigest()[:12]
                            await apply_mb_to_db(
                                old_id    = sid,
                                new_id    = new_sid,
                                file_path = str(new_fp),
                                cdg_path  = None,
                                title     = cur_title,
                                artist    = cur_artist,
                                year      = cur_year or yt_year,
                                genre     = cur_genre or fallback_genre,
                            )
                            log.info("YT fallback rename: %s -> %s", fp.name, new_fp.name)
                            sid = new_sid
                        except OSError as exc:
                            log.warning("YT fallback rename failed: %s", exc)
                else:
                    # No rename needed; still need to backfill year/genre
                    patch: dict = {"metadata_locked": 0}
                    if not cur_year and yt_year:
                        patch["year"] = yt_year
                    if not cur_genre and fallback_genre:
                        patch["genre"] = fallback_genre
                    if len(patch) > 1:
                        await update_song_metadata(sid, patch, force=True)

        # song_id is set last so the frontend always gets the final (renamed) ID.
        # status flips to "done" only *after* song_id is in place — otherwise
        # the frontend poll can race us, see done+no-song_id, and bail.
        job["song_id"] = sid
        job["status"] = "done"
        job["message"] = "Added to library"

    except Exception as exc:
        log.error("Download failed for job %s: %s", job_id, exc)
        job["status"] = "error"
        job["error"] = str(exc)


# Common genre tokens YouTube tags use — maps lowercase key → display form
_GENRE_WHITELIST: dict[str, str] = {
    "rock": "Rock", "pop": "Pop", "country": "Country",
    "country pop": "Country Pop", "country rock": "Country Rock",
    "hip hop": "Hip Hop", "rap": "Rap", "r&b": "R&B", "soul": "Soul",
    "jazz": "Jazz", "blues": "Blues", "folk": "Folk", "metal": "Metal",
    "punk": "Punk", "electronic": "Electronic", "edm": "EDM",
    "dance": "Dance", "house": "House", "techno": "Techno",
    "disco": "Disco", "reggae": "Reggae", "classical": "Classical",
    "indie": "Indie", "indie rock": "Indie Rock", "indie pop": "Indie Pop",
    "alternative": "Alternative", "alternative rock": "Alternative Rock",
    "christian": "Christian", "gospel": "Gospel", "latin": "Latin",
    "k-pop": "K-Pop", "j-pop": "J-Pop", "bluegrass": "Bluegrass",
    "americana": "Americana", "singer-songwriter": "Singer-Songwriter",
    "funk": "Funk",
}


def _guess_genre_from_tags(tags: list) -> str:
    """Pick a genre name from a list of YouTube tags."""
    for t in tags:
        if not isinstance(t, str):
            continue
        display = _GENRE_WHITELIST.get(t.strip().lower())
        if display:
            return display
    return ""


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
    job = _jobs.get(job_id)
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if job is not None and isinstance(info, dict):
            # YouTube Music videos expose track/artist/release_year directly;
            # regular videos give uploader/upload_date/tags we can fall back to.
            job["yt_info"] = {
                "title":        info.get("title", ""),
                "track":        info.get("track") or "",
                "artist":       info.get("artist") or "",
                "album":        info.get("album") or "",
                "uploader":     info.get("uploader") or info.get("channel") or "",
                "release_year": info.get("release_year"),
                "upload_date":  info.get("upload_date") or "",
                "tags":         info.get("tags") or [],
                "categories":   info.get("categories") or [],
            }
