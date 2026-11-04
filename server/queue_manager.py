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

from .config import settings
from .hls_manager import HLSSession, hls_manager
from .stream_manager import stream_manager
from .ws_manager import ws_manager

log = logging.getLogger(__name__)

# Safety timeout: advance queue even if the screen never signals song_ended
# (e.g. screen browser closed mid-song).  2 hours covers the longest karaoke set.
_PLAY_TIMEOUT = 7200


class QueueEntry:
    def __init__(self, song: dict, user: str):
        self.id = str(uuid.uuid4())[:8]
        self.song = song
        self.user = user
        self.server_ts: float = 0.0
        self.play_at: float = 0.0

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
        self._song_ended_event = asyncio.Event()
        self._pitch_change_event = asyncio.Event()
        self._paused: bool = False
        # Live playback state (while _play is running)
        self._current_session: Optional[HLSSession] = None
        self._current_pitch: int = 0

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

    async def pause(self):
        if not self._paused and self._now_playing:
            self._paused = True
            await ws_manager.broadcast({"type": "pause"})
            await self._broadcast_queue()

    async def resume(self):
        if self._paused:
            self._paused = False
            await ws_manager.broadcast({"type": "resume"})
            await self._broadcast_queue()

    def signal_song_ended(self):
        """Called when the screen reports that video playback has finished."""
        self._song_ended_event.set()

    async def set_pitch(self, semitones: int) -> bool:
        """
        Change the pitch of the currently-playing song. Triggers a fresh
        HLS session at the new pitch and a new 'play' broadcast so every
        screen reloads the new playlist in lock-step.
        Returns True if a change was accepted (song is playing, value differs).
        """
        try:
            semitones = int(semitones)
        except (TypeError, ValueError):
            return False
        semitones = max(-12, min(12, semitones))
        if not self._now_playing:
            return False
        if semitones == self._current_pitch:
            return False
        self._current_pitch = semitones
        self._pitch_change_event.set()
        return True

    def get_queue(self) -> list[dict]:
        return [e.to_dict() for e in self._queue]

    def now_playing(self) -> Optional[dict]:
        if self._now_playing:
            e = self._now_playing
            sid = self._current_session.session_id if self._current_session else None
            stream_url = f"/hls/{sid}/index.m3u8" if sid else None
            return {
                **e.to_dict(),
                "stream_url": stream_url,
                "server_ts": e.server_ts,
                "play_at": e.play_at,
                "semitones": self._current_pitch,
                "paused": self._paused,
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
            self._paused = False
            await self._play(entry)
            self._now_playing = None
            self._paused = False

            await ws_manager.broadcast({"type": "stop"})
            await self._broadcast_queue()

    async def _play(self, entry: QueueEntry):
        song = entry.song
        log.info(f"Now playing: {song['title']} (requested by {entry.user})")

        self._current_pitch = 0
        self._skip_event.clear()
        self._song_ended_event.clear()

        # Each loop iteration is one HLS session. A pitch change restarts
        # the song at the new pitch with a fresh session + 'play' broadcast.
        while True:
            self._pitch_change_event.clear()

            session = await hls_manager.start(song, semitones=self._current_pitch)
            ready = await session.wait_ready(timeout=settings.hls_ready_timeout)
            if not ready:
                log.error(
                    f"[hls {session.session_id}] playlist never appeared for "
                    f"{song['id']}; advancing queue"
                )
                await hls_manager.stop(session.session_id)
                return
            self._current_session = session

            now = time.time()
            entry.server_ts = now
            entry.play_at = now + settings.hls_prebuffer_secs
            play_msg = {
                "type": "play",
                "queue_id": entry.id,
                "song": {
                    "id": song["id"],
                    "title": song["title"],
                    "artist": song["artist"],
                    "likes": song["likes"],
                },
                "stream_url": f"/hls/{session.session_id}/index.m3u8",
                "user": entry.user,
                "server_ts": now,
                "play_at": entry.play_at,
                "semitones": self._current_pitch,
                "duration_secs": song.get("duration_secs"),
            }
            await ws_manager.broadcast(play_msg)

            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(self._skip_event.wait()),
                    asyncio.create_task(self._song_ended_event.wait()),
                    asyncio.create_task(self._pitch_change_event.wait()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
                timeout=_PLAY_TIMEOUT,
            )
            for t in pending:
                t.cancel()

            # Tear the session down before any next step (pitch restart or exit).
            await hls_manager.stop(session.session_id)
            self._current_session = None

            if self._pitch_change_event.is_set() and not self._skip_event.is_set():
                log.info(f"[{song['id']}] pitch change → {self._current_pitch:+d}st, restarting")
                continue

            if not done:
                log.warning(
                    f"[{song['id']}] play timeout after {_PLAY_TIMEOUT}s, advancing queue"
                )
            return

    # ── Broadcast helpers ────────────────────────────────────────────────────

    async def _broadcast_queue(self):
        await ws_manager.broadcast({
            "type": "queue_update",
            "queue": self.get_queue(),
            "now_playing": self.now_playing(),
        })


queue_manager = QueueManager()
