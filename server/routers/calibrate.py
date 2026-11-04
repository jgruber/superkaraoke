"""
A/V sync calibration endpoints.

Streams a flash + beep test pattern to **all connected screens** by
enqueueing it as a special song and jumping to it. The operator watches
a real screen (the only place where output-chain latency actually
matters) and nudges the A/V offset until flash and beep look aligned.
"""
import logging

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..queue_manager import queue_manager

router = APIRouter(tags=["calibrate"])
log = logging.getLogger(__name__)


_SYNC_TEST_SONG = {
    "id": "__sync_test__",
    "title": "Sync Test — flash + beep",
    "artist": "SuperKaraoke",
    "kind": "sync_test",
    "path": "",
    "likes": 0,
    "duration_secs": 300,
}


class ApplyRequest(BaseModel):
    offset_ms: int = Field(..., ge=-2000, le=2000)


@router.post("/calibrate/start")
async def calibrate_start() -> dict:
    """Interrupt whatever is playing and run the test on every screen."""
    await queue_manager.enqueue_next(_SYNC_TEST_SONG, "calibration")
    # Skip current if anything is playing so we jump to the sync test.
    if queue_manager.now_playing():
        now = queue_manager.now_playing()
        if now and now.get("song_id") != "__sync_test__":
            await queue_manager.skip()
    log.info("[calibrate] test queued, current av_offset=%d",
             queue_manager.playback_settings().get("av_offset_ms", 0))
    return {"started": True}


@router.post("/calibrate/stop")
async def calibrate_stop() -> dict:
    """End the test if it's playing."""
    now = queue_manager.now_playing()
    if now and now.get("song_id") == "__sync_test__":
        await queue_manager.skip()
    return {"stopped": True}


@router.post("/calibrate/apply")
async def calibrate_apply(req: ApplyRequest) -> dict:
    """Server-side global av_offset_ms setter (same as the WS message)."""
    applied = await queue_manager.set_av_offset(req.offset_ms)
    return {"applied": applied, "offset_ms": req.offset_ms}


@router.get("/calibrate/state")
async def calibrate_state() -> dict:
    """Current settings and whether the sync test is playing right now."""
    s = queue_manager.playback_settings()
    now = queue_manager.now_playing()
    return {
        "av_offset_ms": s["av_offset_ms"],
        "semitones":    s["semitones"],
        "testing":      bool(now and now.get("song_id") == "__sync_test__"),
    }
