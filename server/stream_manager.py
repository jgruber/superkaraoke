"""
Streaming engine.

One ffmpeg subprocess per active song. Its stdout is broadcast to all
connected /stream/{song_id} HTTP clients via per-subscriber asyncio.Queue.
No second port — everything flows through FastAPI's StreamingResponse.

CDG+MP3 is handled by passing both inputs to ffmpeg:
  ffmpeg -i audio.mp3 -i video.cdg ...
Video files are transcoded to fragmented MP4 for browser compatibility.
"""
import asyncio
import logging
import shlex
from asyncio.subprocess import PIPE, DEVNULL
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import settings

log = logging.getLogger(__name__)

# Fragmented MP4 flags required for live HTTP streaming (no seekable moov atom)
FRAG_FLAGS = "frag_keyframe+empty_moov+default_base_moof"


def _cdg_audio_filter(semitones: int) -> list[str]:
    """
    Build -af chain for CDG streams:
      • rubberband pitch shift (if requested)
      • silenceremove to strip trailing silence > 2 s from the MP3
        (karaoke MP3s typically have 3-10 s of dead air baked in)
    Both filters are comma-chained into a single -af argument so they
    don't conflict with each other.
    """
    silence = "silenceremove=stop_periods=-1:stop_duration=2:stop_threshold=-50dB"
    if semitones != 0:
        ratio = 2 ** (semitones / 12)
        chain = f"rubberband=pitch={ratio},{silence}"
    else:
        chain = silence
    return ["-af", chain]


def _video_audio_filter(semitones: int) -> list[str]:
    """
    Build -af chain for video streams:
      • aresample=async=1000  — fills gaps left by dropped/corrupt audio packets
      • rubberband pitch shift — only when semitones != 0
    """
    parts = ["aresample=async=1000"]
    if semitones != 0:
        ratio = 2 ** (semitones / 12)
        parts.append(f"rubberband=pitch={ratio}")
    return ["-af", ",".join(parts)]


def _build_ffmpeg_cmd(song: dict, semitones: int = 0) -> list[str]:
    kind = song["kind"]
    loglevel = settings.ffmpeg_loglevel

    if kind == "cdg":
        audio_path = song["path"]
        cdg_path = song["cdg_path"]
        return [
            "ffmpeg", "-loglevel", loglevel,
            "-i", audio_path,       # input 0: MP3 audio
            "-i", cdg_path,         # input 1: CDG video
            "-map", "1:v",          # video from CDG
            "-map", "0:a",          # audio from MP3
            *_cdg_audio_filter(semitones),
            "-g", "25",             # keyframe every ~1 s (CDG is 25 fps) so the
                                    # last GOP is always ≤ 1 s and gets flushed
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
            "-c:a", "aac", "-b:a", "192k",
            "-f", "mp4", "-movflags", FRAG_FLAGS,
            "pipe:1",
        ]
    else:
        return [
            "ffmpeg", "-loglevel", loglevel,
            "-fflags", "+genpts+discardcorrupt",  # regen PTS; drop bad packets (malformed AVI audio)
            "-err_detect", "ignore_err",           # tolerate decode errors rather than aborting
            "-i", song["path"],
            *_video_audio_filter(semitones),
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
            "-c:a", "aac", "-b:a", "192k", "-ac", "2",
            "-f", "mp4", "-movflags", FRAG_FLAGS,
            "pipe:1",
        ]


# Buffer enough of the stream start for late subscribers to receive the
# initial ftyp+moov boxes.  The moov atom for empty_moov is tiny (~500 B)
# but we keep 512 KB so even slow subscribers that connect a second late
# still get a complete, parseable stream start.
_HEADER_BUF_BYTES = 512 * 1024


@dataclass
class StreamBroadcaster:
    song_id: str
    song: dict
    semitones: int = 0
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    _process: Optional[asyncio.subprocess.Process] = field(default=None, repr=False)
    _task: Optional[asyncio.Task] = field(default=None, repr=False)
    _header_buf: list[bytes] = field(default_factory=list)
    done: bool = False

    def subscribe(self) -> asyncio.Queue:
        # Large queue so ffmpeg can run at full speed and the browser pre-buffers
        # the full stream.  2048 × 64 KB = 128 MB — enough for a ~60-min CDG set.
        q: asyncio.Queue = asyncio.Queue(maxsize=2048)
        # Replay buffered header so late subscribers get a parseable stream start.
        # Safe without locks: asyncio is single-threaded; _pump only mutates
        # _header_buf between its own await points, never concurrently with this.
        for chunk in self._header_buf:
            try:
                q.put_nowait(chunk)
            except asyncio.QueueFull:
                break
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        try:
            self.subscribers.remove(q)
        except ValueError:
            pass

    async def _pump(self):
        cmd = _build_ffmpeg_cmd(self.song, self.semitones)
        label = f"{self.song_id}+{self.semitones:+d}st" if self.semitones else self.song_id
        log.info(f"[{label}] ffmpeg start: {shlex.join(cmd)}")
        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd, stdin=DEVNULL, stdout=PIPE, stderr=PIPE
            )
            buf_size = 0
            chunk_count = 0
            stderr_chunks: list[bytes] = []

            async def _drain_stderr():
                while True:
                    piece = await self._process.stderr.read(4096)
                    if not piece:
                        break
                    stderr_chunks.append(piece)

            stderr_task = asyncio.create_task(_drain_stderr())

            while True:
                chunk = await self._process.stdout.read(settings.stream_chunk_size)
                if not chunk:
                    break
                chunk_count += 1
                # Buffer stream start so late-joining subscribers receive the
                # initial ftyp+moov boxes and can parse the fragmented MP4.
                if buf_size < _HEADER_BUF_BYTES:
                    self._header_buf.append(chunk)
                    buf_size += len(chunk)
                # Distribute to all subscribers; drop slow ones to avoid backpressure
                for q in list(self.subscribers):
                    try:
                        q.put_nowait(chunk)
                    except asyncio.QueueFull:
                        log.warning(f"[{label}] subscriber queue full, dropping chunk")

            await stderr_task
            await self._process.wait()
            log.info(
                f"[{label}] ffmpeg exited with code {self._process.returncode} "
                f"after {chunk_count} chunks ({buf_size} bytes buffered)"
            )
            if stderr_chunks:
                log.warning(f"[{label}] ffmpeg stderr: {b''.join(stderr_chunks).decode(errors='replace')}")
        except Exception as e:
            log.error(f"[{self.song_id}] ffmpeg pump error: {e}")
        finally:
            self.done = True
            # Send sentinel None to all subscribers so their generators terminate
            for q in list(self.subscribers):
                try:
                    q.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    def start(self):
        self._task = asyncio.create_task(self._pump())

    async def stop(self):
        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._process.kill()
        if self._task:
            await asyncio.shield(self._task)


class StreamManager:
    def __init__(self):
        # Keyed by (song_id, semitones) so each pitch offset gets its own broadcaster
        self._active: dict[tuple[str, int], StreamBroadcaster] = {}

    def start_stream(self, song: dict, semitones: int = 0) -> StreamBroadcaster:
        """Start (or return existing) broadcaster for this song+semitones pair."""
        key = (song["id"], semitones)
        if key in self._active:
            asyncio.create_task(self._active[key].stop())

        broadcaster = StreamBroadcaster(song_id=song["id"], song=song, semitones=semitones)
        broadcaster.start()
        self._active[key] = broadcaster
        return broadcaster

    def get_or_start_stream(self, song: dict, semitones: int = 0) -> StreamBroadcaster:
        """Return existing broadcaster or start a new one on demand."""
        key = (song["id"], semitones)
        if key not in self._active or self._active[key].done:
            return self.start_stream(song, semitones)
        return self._active[key]

    def get_broadcaster(self, song_id: str, semitones: int = 0) -> Optional[StreamBroadcaster]:
        return self._active.get((song_id, semitones))

    async def stop_all_for_song(self, song_id: str):
        """Stop every semitone variant for a given song (called when queue advances)."""
        keys = [k for k in self._active if k[0] == song_id]
        for key in keys:
            await self._active.pop(key).stop()

    async def shutdown(self):
        for bc in list(self._active.values()):
            await bc.stop()
        self._active.clear()


stream_manager = StreamManager()
