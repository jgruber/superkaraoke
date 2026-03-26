from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from ..stream_manager import stream_manager
from ..library import library

router = APIRouter(tags=["stream"])

_SEMITONE_LIMIT = 12  # ±12 semitones (one octave each way)


@router.get("/stream/{song_id}")
async def stream_song(
    song_id: str,
    semitones: int = Query(0, ge=-_SEMITONE_LIMIT, le=_SEMITONE_LIMIT,
                           description="Pitch shift in semitones (−12 to +12)"),
):
    """
    Subscribe to the broadcast stream for song_id at the requested pitch.
    Screens at semitones=0 share the default broadcaster.
    Screens at any other value get their own dedicated ffmpeg process.
    """
    song = await library.get(song_id)
    if not song:
        raise HTTPException(status_code=404, detail="Song not found")

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
        },
    )
