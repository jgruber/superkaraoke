from fastapi import APIRouter, HTTPException, Query
from ..library import library
from ..database import increment_like

router = APIRouter(tags=["songs"])


@router.get("/songs")
async def list_songs(
    q: str = Query("", description="Search query"),
    sort: str = Query("title", description="Sort by: title | artist | likes | year | genre"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    results, total = await library.search(q, sort=sort, limit=limit, offset=offset)
    return {"songs": results, "total": total, "offset": offset, "limit": limit}


@router.get("/songs/{song_id}")
async def get_song(song_id: str):
    song = await library.get(song_id)
    if not song:
        raise HTTPException(status_code=404, detail="Song not found")
    return song


@router.post("/songs/{song_id}/like")
async def like_song(song_id: str):
    song = await library.get(song_id)
    if not song:
        raise HTTPException(status_code=404, detail="Song not found")
    new_count = await increment_like(song_id, delta=1)
    return {"song_id": song_id, "likes": new_count}


@router.delete("/songs/{song_id}/like")
async def unlike_song(song_id: str):
    song = await library.get(song_id)
    if not song:
        raise HTTPException(status_code=404, detail="Song not found")
    new_count = await increment_like(song_id, delta=-1)
    return {"song_id": song_id, "likes": new_count}
