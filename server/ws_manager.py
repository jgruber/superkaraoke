"""
WebSocket connection manager.

Clients identify themselves on connect with a JSON message:
  {"type": "hello", "client_type": "screen" | "user", "name": "optional"}

Server broadcasts state updates to all connected clients:
  {"type": "state", "queue": [...], "now_playing": {...} | null}

When a song starts:
  {"type": "play", "song": {...}, "stream_url": "/stream/<id>", "server_ts": 1234567890.123}

When the queue is updated:
  {"type": "queue_update", "queue": [...]}

When playback ends:
  {"type": "stop"}
"""
import asyncio
import json
import logging
import time
from typing import Optional

from fastapi import WebSocket

log = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self):
        self._connections: dict[WebSocket, dict] = {}  # ws -> client info

    async def connect(self, ws: WebSocket, client_type: str = "user", name: str = ""):
        await ws.accept()
        self._connections[ws] = {"client_type": client_type, "name": name}
        log.info(f"WS connect: {client_type} '{name}' (total={len(self._connections)})")

    def disconnect(self, ws: WebSocket):
        self._connections.pop(ws, None)
        log.info(f"WS disconnect (total={len(self._connections)})")

    async def broadcast(self, message: dict, client_type: Optional[str] = None):
        """Send message to all (or filtered by client_type) connections."""
        payload = json.dumps(message)
        dead = []
        for ws, info in list(self._connections.items()):
            if client_type and info["client_type"] != client_type:
                continue
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def send(self, ws: WebSocket, message: dict):
        try:
            await ws.send_text(json.dumps(message))
        except Exception:
            self.disconnect(ws)

    def screen_count(self) -> int:
        return sum(1 for info in self._connections.values() if info["client_type"] == "screen")

    def user_count(self) -> int:
        return sum(1 for info in self._connections.values() if info["client_type"] == "user")


ws_manager = ConnectionManager()
