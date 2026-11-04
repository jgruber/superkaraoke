"""
HLS session manager.

For each playing song, spin up a dedicated ffmpeg process that produces a
live HLS stream (fragmented-MP4 segments + a growing .m3u8 playlist) in a
per-session scratch directory.  All connected screens load the same
playlist and therefore play the same segments at roughly the same
wall-clock time, with PROGRAM-DATE-TIME anchoring each segment.

ffmpeg is launched with ``-re`` so it paces input at native frame rate:
late-joining screens see a playlist that reflects the real song position
rather than a playlist whose segments were produced faster than real time.

Sessions are addressed by a random ``session_id`` so stale playlists
from a previous song can never leak to clients.
"""
import asyncio
import logging
import re
import shlex
import shutil
import time
import uuid
from asyncio.subprocess import DEVNULL, PIPE
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import settings

log = logging.getLogger(__name__)


def _cdg_audio_filter(semitones: int) -> list[str]:
    silence = "silenceremove=stop_periods=-1:stop_duration=2:stop_threshold=-50dB"
    if semitones != 0:
        ratio = 2 ** (semitones / 12)
        chain = f"rubberband=pitch={ratio},{silence}"
    else:
        chain = silence
    return ["-af", chain]


def _video_audio_filter(semitones: int) -> list[str]:
    parts = ["aresample=async=1000"]
    if semitones != 0:
        ratio = 2 ** (semitones / 12)
        parts.append(f"rubberband=pitch={ratio}")
    return ["-af", ",".join(parts)]


def _hls_output_args(out_dir: Path) -> list[str]:
    seg_dur = settings.hls_segment_duration
    return [
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-g", str(max(1, int(round(seg_dur * 25)))),  # one keyframe per segment at 25 fps
        "-sc_threshold", "0",                          # disable scene-change keyframes
        "-c:a", "aac", "-b:a", "192k", "-ac", "2",
        "-f", "hls",
        "-hls_time", f"{seg_dur:g}",
        "-hls_list_size", "0",                          # keep every segment
        "-hls_segment_type", "fmp4",
        "-hls_fmp4_init_filename", "init.mp4",
        "-hls_flags",
        "independent_segments+program_date_time+append_list+omit_endlist+temp_file",
        "-hls_playlist_type", "event",
        "-hls_segment_filename", str(out_dir / "seg_%06d.m4s"),
        str(out_dir / "index.m3u8"),
    ]


def _build_ffmpeg_cmd(song: dict, semitones: int, out_dir: Path) -> list[str]:
    kind = song["kind"]
    loglevel = settings.ffmpeg_loglevel

    if kind == "cdg":
        return [
            "ffmpeg", "-loglevel", loglevel,
            "-re", "-i", song["path"],         # input 0: MP3 audio (paced real-time)
            "-re", "-i", song["cdg_path"],     # input 1: CDG video (paced real-time)
            "-map", "1:v", "-map", "0:a",
            *_cdg_audio_filter(semitones),
            *_hls_output_args(out_dir),
        ]
    return [
        "ffmpeg", "-loglevel", loglevel,
        "-re",
        "-fflags", "+genpts+discardcorrupt",
        "-err_detect", "ignore_err",
        "-i", song["path"],
        *_video_audio_filter(semitones),
        *_hls_output_args(out_dir),
    ]


_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


def is_safe_filename(name: str) -> bool:
    """Guard against path-traversal when serving files from a session dir."""
    return bool(_SAFE_NAME.fullmatch(name)) and ".." not in name


@dataclass
class HLSSession:
    song_id: str
    song: dict
    semitones: int
    session_id: str
    out_dir: Path
    _process: Optional[asyncio.subprocess.Process] = field(default=None, repr=False)
    _reap_task: Optional[asyncio.Task] = field(default=None, repr=False)
    started_at: float = 0.0  # Unix seconds at ffmpeg launch
    done: bool = False

    async def start(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.started_at = time.time()
        cmd = _build_ffmpeg_cmd(self.song, self.semitones, self.out_dir)
        label = f"{self.song_id}+{self.semitones:+d}st" if self.semitones else self.song_id
        log.info(f"[hls {self.session_id} {label}] ffmpeg start: {shlex.join(cmd)}")
        self._process = await asyncio.create_subprocess_exec(
            *cmd, stdin=DEVNULL, stdout=DEVNULL, stderr=PIPE,
        )
        self._reap_task = asyncio.create_task(self._reap())

    async def _reap(self) -> None:
        stderr_chunks: list[bytes] = []
        assert self._process and self._process.stderr
        while True:
            piece = await self._process.stderr.read(4096)
            if not piece:
                break
            stderr_chunks.append(piece)
        await self._process.wait()
        self.done = True
        rc = self._process.returncode
        log.info(f"[hls {self.session_id}] ffmpeg exited rc={rc}")
        if stderr_chunks and rc not in (0, -15, 255):
            log.warning(
                f"[hls {self.session_id}] stderr: "
                f"{b''.join(stderr_chunks).decode(errors='replace')}"
            )

    async def wait_ready(self, timeout: float = 8.0) -> bool:
        """
        Block until the playlist exists and lists at least one segment —
        otherwise hls.js will see an empty playlist and spin on retries.
        """
        playlist = self.out_dir / "index.m3u8"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.done:
                return False
            if playlist.exists():
                try:
                    text = playlist.read_text()
                except OSError:
                    text = ""
                if ".m4s" in text:
                    return True
            await asyncio.sleep(0.05)
        return False

    async def stop(self) -> None:
        if self._process and self._process.returncode is None:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                try:
                    self._process.kill()
                except ProcessLookupError:
                    pass
            except ProcessLookupError:
                pass
        if self._reap_task:
            try:
                await asyncio.shield(self._reap_task)
            except asyncio.CancelledError:
                pass
        # Let in-flight segment requests finish before we wipe the dir.
        await asyncio.sleep(0.5)
        shutil.rmtree(self.out_dir, ignore_errors=True)


class HLSManager:
    def __init__(self) -> None:
        self._sessions: dict[str, HLSSession] = {}

    def _session_dir(self, session_id: str) -> Path:
        return settings.hls_dir / session_id

    async def start(self, song: dict, semitones: int = 0) -> HLSSession:
        session_id = uuid.uuid4().hex[:12]
        session = HLSSession(
            song_id=song["id"],
            song=song,
            semitones=semitones,
            session_id=session_id,
            out_dir=self._session_dir(session_id),
        )
        self._sessions[session_id] = session
        await session.start()
        return session

    def get(self, session_id: str) -> Optional[HLSSession]:
        return self._sessions.get(session_id)

    async def stop(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session:
            await session.stop()

    async def shutdown(self) -> None:
        for sid in list(self._sessions):
            await self.stop(sid)
        try:
            if settings.hls_dir.exists():
                for child in settings.hls_dir.iterdir():
                    shutil.rmtree(child, ignore_errors=True)
        except OSError:
            pass


hls_manager = HLSManager()
