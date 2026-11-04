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


def _audio_chain(semitones: int, av_offset_ms: int, pre: str = "") -> str:
    """Build an -af filter chain string: optional existing stage, optional
    pitch shift, optional positive audio delay. Empty string = no filter."""
    parts: list[str] = []
    if pre:
        parts.append(pre)
    if semitones != 0:
        ratio = 2 ** (semitones / 12)
        parts.append(f"rubberband=pitch={ratio}")
    if av_offset_ms > 0:
        parts.append(f"adelay={av_offset_ms}:all=1")
    return ",".join(parts)


def _video_chain(av_offset_ms: int) -> str:
    """Pad video start when audio needs to be advanced (av_offset_ms < 0)."""
    if av_offset_ms >= 0:
        return ""
    return f"tpad=start_duration={-av_offset_ms / 1000:g}:start_mode=add:color=black"


def _cdg_filters(semitones: int, av_offset_ms: int) -> list[str]:
    # Strip trailing silence from the backing MP3 (karaoke tracks often
    # have several seconds of dead air baked in).
    audio = _audio_chain(
        semitones, av_offset_ms,
        pre="silenceremove=stop_periods=-1:stop_duration=2:stop_threshold=-50dB",
    )
    video = _video_chain(av_offset_ms)
    args: list[str] = []
    if audio:
        args += ["-af", audio]
    if video:
        args += ["-vf", video]
    return args


def _video_filters(semitones: int, av_offset_ms: int) -> list[str]:
    audio = _audio_chain(semitones, av_offset_ms)
    video = _video_chain(av_offset_ms)
    args: list[str] = []
    if audio:
        args += ["-af", audio]
    if video:
        args += ["-vf", video]
    return args


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


_SYNC_TEST_SECS = 300


def _build_sync_test_cmd(out_dir: Path) -> list[str]:
    """
    30-second flash+beep sync-test stream produced via lavfi. A white
    flash fills the frame for 50 ms at each integer second; a 50 ms burst
    of a 1 kHz tone fires at the same instant. Source alignment is
    perfect, so any delay observed at the <video> element is the pipeline
    (+ output chain) offset — exactly the number we want to calibrate.
    """
    loglevel = settings.ffmpeg_loglevel
    d = _SYNC_TEST_SECS
    return [
        "ffmpeg", "-loglevel", loglevel,
        "-f", "lavfi", "-i", f"color=c=black:s=1280x720:r=30:d={d}",
        "-f", "lavfi", "-i", f"sine=f=1000:d={d}",
        "-vf",
        "drawbox=x=0:y=0:w=iw:h=ih:color=white@1:t=fill:enable='lt(mod(t,1),0.05)',"
        "drawtext=text='SYNC TEST':fontcolor=yellow:fontsize=48:x=20:y=20",
        # Faint continuous 1 kHz carrier (so RMS has a nonzero baseline
        # when the audio tap is wired up correctly) with loud 50 ms
        # pulses at each integer second.
        "-af", "volume='if(lt(mod(t,1),0.05),0.8,0.05)':eval=frame",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-g", "15", "-sc_threshold", "0", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2",
        "-f", "hls",
        "-hls_time", f"{settings.hls_segment_duration:g}",
        "-hls_list_size", "0",
        "-hls_segment_type", "fmp4",
        "-hls_fmp4_init_filename", "init.mp4",
        "-hls_flags",
        "independent_segments+program_date_time+append_list+omit_endlist+temp_file",
        "-hls_playlist_type", "event",
        "-hls_segment_filename", str(out_dir / "seg_%06d.m4s"),
        str(out_dir / "index.m3u8"),
    ]


def _build_ffmpeg_cmd(
    song: dict, semitones: int, av_offset_ms: int, out_dir: Path
) -> list[str]:
    kind = song["kind"]
    loglevel = settings.ffmpeg_loglevel

    if kind == "sync_test":
        return _build_sync_test_cmd(out_dir)

    # Produce segments at native encoder speed (no -re). The playlist
    # fills ahead of real-time playback, so hls.js's live-sync heuristics
    # never pull currentTime back toward the live edge. Once ffmpeg exits,
    # we append #EXT-X-ENDLIST and the client transitions fully to VOD.
    if kind == "cdg":
        return [
            "ffmpeg", "-loglevel", loglevel,
            "-i", song["path"],
            "-i", song["cdg_path"],
            "-map", "1:v", "-map", "0:a",
            *_cdg_filters(semitones, av_offset_ms),
            *_hls_output_args(out_dir),
        ]
    # AVI inputs are the only ones that actually need the discard/error-
    # tolerance flags; applying them to every source can also perturb the
    # audio timeline because +genpts synthesises missing PTS from DTS.
    is_avi = song["path"].lower().endswith(".avi")
    resilience = (
        ["-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err"]
        if is_avi else []
    )
    return [
        "ffmpeg", "-loglevel", loglevel,
        *resilience,
        "-i", song["path"],
        *_video_filters(semitones, av_offset_ms),
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
    av_offset_ms: int
    session_id: str
    out_dir: Path
    _process: Optional[asyncio.subprocess.Process] = field(default=None, repr=False)
    _reap_task: Optional[asyncio.Task] = field(default=None, repr=False)
    started_at: float = 0.0  # Unix seconds at ffmpeg launch
    done: bool = False

    async def start(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.started_at = time.time()
        cmd = _build_ffmpeg_cmd(self.song, self.semitones, self.av_offset_ms, self.out_dir)
        bits = [self.song_id]
        if self.semitones:
            bits.append(f"{self.semitones:+d}st")
        if self.av_offset_ms:
            bits.append(f"{self.av_offset_ms:+d}ms")
        label = " ".join(bits)
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
        rc = self._process.returncode
        # On normal completion, mark the playlist as VOD so hls.js knows
        # the song is finished — otherwise the stream is treated as live
        # forever and <video>.onended / BUFFER_EOS never fire.
        if rc == 0:
            self._finalize_playlist()
        self.done = True
        log.info(f"[hls {self.session_id}] ffmpeg exited rc={rc}")
        if stderr_chunks and rc not in (0, -15, 255):
            log.warning(
                f"[hls {self.session_id}] stderr: "
                f"{b''.join(stderr_chunks).decode(errors='replace')}"
            )

    def _finalize_playlist(self) -> None:
        playlist = self.out_dir / "index.m3u8"
        try:
            text = playlist.read_text()
        except OSError:
            return
        if "#EXT-X-ENDLIST" in text:
            return
        if not text.endswith("\n"):
            text += "\n"
        text += "#EXT-X-ENDLIST\n"
        try:
            playlist.write_text(text)
        except OSError as e:
            log.warning(f"[hls {self.session_id}] endlist write failed: {e}")

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

    async def start(
        self, song: dict, semitones: int = 0, av_offset_ms: int = 0
    ) -> HLSSession:
        session_id = uuid.uuid4().hex[:12]
        session = HLSSession(
            song_id=song["id"],
            song=song,
            semitones=semitones,
            av_offset_ms=av_offset_ms,
            session_id=session_id,
            out_dir=self._session_dir(session_id),
        )
        self._sessions[session_id] = session
        await session.start()
        return session

    async def start_sync_test(self) -> HLSSession:
        """Spawn an HLS session that streams a flash+beep calibration pattern."""
        song = {
            "id": "__sync_test__",
            "title": "Sync Test",
            "artist": "SuperKaraoke",
            "kind": "sync_test",
            "path": "",
        }
        return await self.start(song, semitones=0, av_offset_ms=0)

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
