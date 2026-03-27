from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from ..stream_manager import stream_manager
from ..library import library

router = APIRouter(tags=["stream"])

_SEMITONE_LIMIT = 12  # ±12 semitones (one octave each way)

# Only these containers are natively playable in browsers without transcoding.
_BROWSER_SAFE_EXTS = {".mp4", ".webm"}


@router.get("/stream/{song_id}")
async def stream_song(
    song_id: str,
    semitones: int = Query(0, ge=-_SEMITONE_LIMIT, le=_SEMITONE_LIMIT,
                           description="Pitch shift in semitones (−12 to +12)"),
):
    song = await library.get(song_id)
    if not song:
        raise HTTPException(status_code=404, detail="Song not found")

    # Browser-safe video files at base pitch: serve directly with Range support.
    # AVI, MKV, MOV, etc. are not browser-playable and fall through to ffmpeg.
    path = Path(song["path"])
    if song["kind"] == "video" and semitones == 0 and path.suffix.lower() in _BROWSER_SAFE_EXTS:
        return FileResponse(str(path), media_type="video/mp4" if path.suffix.lower() == ".mp4" else "video/webm")

    # CDG files or pitch-shifted streams: transcode via ffmpeg broadcaster.
    # ffmpeg runs at full speed; the browser downloads the whole stream into
    # its buffer and plays smoothly, then fires 'ended' when done.
    broadcaster = stream_manager.get_or_start_stream(song, semitones)
    q = broadcaster.subscribe()

    async def generate():
        try:
            while True:
                chunk = await q.get()
                if chunk is None:   # sentinel: stream ended
                    break
                yield chunk
        except Exception:
            pass
        finally:
            broadcaster.unsubscribe(q)

    return StreamingResponse(
        generate(),
        media_type="video/mp4",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Content-Type-Options": "nosniff",
            "Accept-Ranges": "none",
        },
    )
