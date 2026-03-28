from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..auth import get_session_user
from ..queue_manager import queue_manager
from ..library import library

router = APIRouter(tags=["queue"])


class EnqueueRequest(BaseModel):
    song_id: str
    user: str = "anonymous"
    play_next: bool = False


@router.get("/queue")
async def get_queue():
    return {
        "queue": queue_manager.get_queue(),
        "now_playing": queue_manager.now_playing(),
    }


@router.post("/queue")
async def enqueue(req: EnqueueRequest, request: Request):
    song = await library.get(req.song_id)
    if not song:
        raise HTTPException(status_code=404, detail="Song not found")
    # Authenticated session username always wins over the client-supplied value
    user = get_session_user(request) or req.user
    if req.play_next:
        entry = await queue_manager.enqueue_next(song, user)
    else:
        entry = await queue_manager.enqueue(song, user)
    return entry.to_dict()


@router.delete("/queue/{queue_id}")
async def remove_from_queue(queue_id: str):
    removed = await queue_manager.remove(queue_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Queue entry not found")
    return {"ok": True}


@router.post("/queue/{queue_id}/move-up")
async def move_up(queue_id: str):
    ok = await queue_manager.move_up(queue_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Cannot move up")
    return {"ok": True}


@router.post("/queue/{queue_id}/move-down")
async def move_down(queue_id: str):
    ok = await queue_manager.move_down(queue_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Cannot move down")
    return {"ok": True}


@router.post("/queue/skip")
async def skip():
    await queue_manager.skip()
    return {"ok": True}


@router.post("/queue/pause")
async def pause():
    await queue_manager.pause()
    return {"ok": True, "paused": True}


@router.post("/queue/resume")
async def resume():
    await queue_manager.resume()
    return {"ok": True, "paused": False}
