import json
import logging

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
    except Exception:
        client_type = "user"
        name = ""

    # Re-register with proper metadata (already accepted above, update registry)
    ws_manager._connections[websocket] = {"client_type": client_type, "name": name}
    log.info(f"WS registered: {client_type} '{name}'")

    # Send current state immediately on connect
    await ws_manager.send(websocket, {
        "type": "state",
        "queue": queue_manager.get_queue(),
        "now_playing": queue_manager.now_playing(),
    })

    try:
        while True:
            raw = await websocket.receive_text()
            # Clients can send pings or other messages; currently just ignore
            try:
                msg = json.loads(raw)
                if msg.get("type") == "ping":
                    await ws_manager.send(websocket, {"type": "pong"})
            except Exception:
                pass
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
