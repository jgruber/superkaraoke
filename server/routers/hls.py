"""
HLS playlist + segment serving.

Each playing song gets a session directory under ``settings.hls_dir``;
this router exposes the playlist and the fragments inside it, scoped by
the session id so stale files from a previous song can never leak.
"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..hls_manager import hls_manager, is_safe_filename

router = APIRouter(tags=["hls"])


_MEDIA_TYPES: dict[str, str] = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".m4s":  "video/iso.segment",
    ".mp4":  "video/mp4",
}


@router.get("/hls/{session_id}/{filename}")
async def hls_file(session_id: str, filename: str):
    if not is_safe_filename(session_id) or not is_safe_filename(filename):
        raise HTTPException(status_code=400, detail="Bad name")

    session = hls_manager.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    path = session.out_dir / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    suffix = path.suffix.lower()
    media_type = _MEDIA_TYPES.get(suffix, "application/octet-stream")

    if suffix == ".m3u8":
        headers = {"Cache-Control": "no-cache, no-store"}
    else:
        headers = {"Cache-Control": "public, max-age=3600, immutable"}

    return FileResponse(str(path), media_type=media_type, headers=headers)
