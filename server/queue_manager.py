"""
In-memory song queue with async-safe operations.
Drives the playback loop: dequeues songs, starts ffmpeg streams,
broadcasts play/stop events over WebSocket.
"""
import asyncio
import logging
import time
import uuid
from typing import Optional

from .stream_manager import stream_manager
from .ws_manager import ws_manager

log = logging.getLogger(__name__)


class QueueEntry:
    def __init__(self, song: dict, user: str):
        self.id = str(uuid.uuid4())[:8]
        self.song = song
        self.user = user

    def to_dict(self) -> dict:
        return {
            "queue_id": self.id,
            "song_id": self.song["id"],
            "title": self.song["title"],
            "artist": self.song["artist"],
            "user": self.user,
        }


class QueueManager:
    def __init__(self):
        self._queue: list[QueueEntry] = []
        self._lock = asyncio.Lock()
        self._event = asyncio.Event()
        self._now_playing: Optional[QueueEntry] = None
        self._playback_task: Optional[asyncio.Task] = None
        self._skip_event = asyncio.Event()

    # ── Queue mutations ──────────────────────────────────────────────────────

    async def enqueue(self, song: dict, user: str) -> QueueEntry:
        entry = QueueEntry(song, user)
        async with self._lock:
            self._queue.append(entry)
            self._event.set()
        await self._broadcast_queue()
        return entry

    async def enqueue_next(self, song: dict, user: str) -> QueueEntry:
        """Insert at front of queue (play next)."""
        entry = QueueEntry(song, user)
        async with self._lock:
            self._queue.insert(0, entry)
            self._event.set()
        await self._broadcast_queue()
        return entry

    async def remove(self, queue_id: str) -> bool:
        async with self._lock:
            before = len(self._queue)
            self._queue = [e for e in self._queue if e.id != queue_id]
            removed = len(self._queue) < before
        if removed:
            await self._broadcast_queue()
        return removed

    async def move_up(self, queue_id: str) -> bool:
        async with self._lock:
            idx = next((i for i, e in enumerate(self._queue) if e.id == queue_id), None)
            if idx is None or idx == 0:
                return False
            self._queue[idx - 1], self._queue[idx] = self._queue[idx], self._queue[idx - 1]
        await self._broadcast_queue()
        return True

    async def move_down(self, queue_id: str) -> bool:
        async with self._lock:
            idx = next((i for i, e in enumerate(self._queue) if e.id == queue_id), None)
            if idx is None or idx >= len(self._queue) - 1:
                return False
            self._queue[idx], self._queue[idx + 1] = self._queue[idx + 1], self._queue[idx]
        await self._broadcast_queue()
        return True

    async def skip(self):
        self._skip_event.set()

    def get_queue(self) -> list[dict]:
        return [e.to_dict() for e in self._queue]

    def now_playing(self) -> Optional[dict]:
        if self._now_playing:
            return {
                **self._now_playing.to_dict(),
                "stream_url": f"/stream/{self._now_playing.song['id']}",
            }
        return None

    # ── Playback loop ────────────────────────────────────────────────────────

    def start_loop(self):
        self._playback_task = asyncio.create_task(self._loop())

    async def _loop(self):
        log.info("Playback loop started")
        while True:
            # Wait for something in the queue
            await self._event.wait()

            async with self._lock:
                if not self._queue:
                    self._event.clear()
                    continue
                entry = self._queue.pop(0)
                if not self._queue:
                    self._event.clear()

            self._now_playing = entry
            await self._play(entry)
            self._now_playing = None

            await ws_manager.broadcast({"type": "stop"})
            await self._broadcast_queue()

    async def _play(self, entry: QueueEntry):
        song = entry.song
        log.info(f"Now playing: {song['title']} (requested by {entry.user})")

        # Start the default (semitones=0) broadcaster; pitched variants spawn on demand
        broadcaster = stream_manager.start_stream(song, semitones=0)

        stream_url = f"/stream/{song['id']}"
        play_msg = {
            "type": "play",
            "queue_id": entry.id,
            "song": {
                "id": song["id"],
                "title": song["title"],
                "artist": song["artist"],
                "likes": song["likes"],
            },
            "stream_url": stream_url,
            "user": entry.user,
            "server_ts": time.time(),
        }
        await ws_manager.broadcast(play_msg)

        # Wait until ffmpeg finishes or skip is requested
        self._skip_event.clear()
        done_task = asyncio.create_task(_wait_broadcaster_done(broadcaster))
        skip_task = asyncio.create_task(self._skip_event.wait())

        _, pending = await asyncio.wait(
            [done_task, skip_task], return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()

        # Stop every semitone variant for this song
        await stream_manager.stop_all_for_song(song["id"])

    # ── Broadcast helpers ────────────────────────────────────────────────────

    async def _broadcast_queue(self):
        await ws_manager.broadcast({
            "type": "queue_update",
            "queue": self.get_queue(),
            "now_playing": self.now_playing(),
        })


async def _wait_broadcaster_done(broadcaster):
    while not broadcaster.done:
        await asyncio.sleep(0.5)


queue_manager = QueueManager()
