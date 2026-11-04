import json
import logging
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..ws_manager import ws_manager
from ..queue_manager import queue_manager

router = APIRouter(tags=["websocket"])
log = logging.getLogger(__name__)


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # Accept with a temporary connection, wait for hello
    await websocket.accept()

    # Expect first message to identify client type
    try:
        raw = await websocket.receive_text()
        data = json.loads(raw)
        client_type = data.get("client_type", "user")
        name = data.get("name", "")
    except WebSocketDisconnect:
        return
    except Exception:
        client_type = "user"
        name = ""

    # Re-register with proper metadata (already accepted above, update registry)
    ws_manager._connections[websocket] = {"client_type": client_type, "name": name}
    log.info(f"WS registered: {client_type} '{name}'")

    # A screen just (re)appeared — cancel any pending "no screens" skip.
    if client_type == "screen":
        queue_manager.handle_screen_count_change(ws_manager.screen_count())

    # Send current state immediately on connect
    await ws_manager.send(websocket, {
        "type": "state",
        "queue": queue_manager.get_queue(),
        "now_playing": queue_manager.now_playing(),
    })

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
                msg_type = msg.get("type")
                if msg_type == "ping":
                    await ws_manager.send(websocket, {"type": "pong"})
                elif msg_type == "time_sync":
                    # NTP-lite: client sends t1 (client ms), server stamps
                    # t2/t3 (server ms) so the client can compute its own
                    # clock offset relative to the server.
                    now_ms = time.time() * 1000.0
                    await ws_manager.send(websocket, {
                        "type": "time_sync",
                        "t1": msg.get("t1"),
                        "t2": now_ms,
                        "t3": now_ms,
                    })
                elif msg_type == "song_ended":
                    # Screen reports that video playback has finished naturally
                    queue_manager.signal_song_ended()
                elif msg_type == "set_pitch":
                    # Screen asks the server to re-encode at a new pitch; all
                    # screens receive a fresh 'play' message and reload.
                    await queue_manager.set_pitch(msg.get("semitones", 0))
            except Exception:
                pass
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
        if client_type == "screen":
            queue_manager.handle_screen_count_change(ws_manager.screen_count())
