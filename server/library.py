"""
Karaoke library: syncs filesystem → SQLite on startup and on file changes.

The database is the source of truth for all song metadata.
This module owns the scan loop and file watcher.
"""
import asyncio
import hashlib
import logging
import threading
from pathlib import Path
from typing import Optional

from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers import Observer

from .config import settings
from .database import (
    upsert_song, get_song, search_songs, count_songs, remove_missing_songs,
)
from .metadata import extract_metadata

log = logging.getLogger(__name__)

VIDEO_EXTS = set(settings.supported_video_extensions)
AUDIO_EXTS = set(settings.supported_audio_extensions)
CDG_EXTS   = set(settings.supported_cdg_extensions)


def _song_id(path: Path) -> str:
    """Stable 12-char ID: sha256 of path relative to media_dir."""
    rel = str(path.relative_to(settings.media_dir))
    return hashlib.sha256(rel.encode()).hexdigest()[:12]


class Library:
    def __init__(self):
        self._song_count: int = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._observer: Optional[Observer] = None
        self._debounce_timer: Optional[threading.Timer] = None

    # ── Scanning ──────────────────────────────────────────────────────────────

    async def scan(self, overwrite_metadata: bool = False):
        """
        Walk media_dir, upsert every found song into the DB, then remove
        DB entries for files that no longer exist.

        overwrite_metadata=True is only used when the user explicitly
        triggers a forced re-detect from the library management UI.
        """
        self._loop = asyncio.get_running_loop()
        media_dir = settings.media_dir

        if not media_dir.exists():
            log.warning("Media dir does not exist: %s", media_dir)
            self._song_count = 0
            return

        found_ids: set[str] = set()

        # Group files by directory so CDG pairing is per-directory
        by_dir: dict[Path, list[Path]] = {}
        for p in media_dir.rglob("*"):
            if p.is_file():
                by_dir.setdefault(p.parent, []).append(p)

        for files in by_dir.values():
            cdg_map = {
                f.stem.lower(): f
                for f in files
                if f.suffix.lower() in CDG_EXTS
            }

            for f in files:
                ext = f.suffix.lower()

                if ext in AUDIO_EXTS:
                    cdg = cdg_map.get(f.stem.lower())
                    if not cdg:
                        continue
                    sid = _song_id(f)
                    meta = await asyncio.to_thread(
                        extract_metadata, str(f), str(cdg), "cdg"
                    )
                    song_data = {
                        "id": sid,
                        "file_path": str(f),
                        "cdg_path": str(cdg),
                        "kind": "cdg",
                        **meta,
                    }

                elif ext in VIDEO_EXTS:
                    sid = _song_id(f)
                    meta = await asyncio.to_thread(
                        extract_metadata, str(f), None, "video"
                    )
                    song_data = {
                        "id": sid,
                        "file_path": str(f),
                        "cdg_path": None,
                        "kind": "video",
                        **meta,
                    }

                else:
                    continue

                await upsert_song(song_data)
                found_ids.add(song_data["id"])

        await remove_missing_songs(found_ids)
        self._song_count = await count_songs()
        log.info("Scan complete: %d songs in %s", self._song_count, media_dir)

    # ── Lookup API ────────────────────────────────────────────────────────────

    async def get(self, song_id: str) -> Optional[dict]:
        return await get_song(song_id)

    async def search(
        self,
        query: str = "",
        sort: str = "title",
        limit: int = 50,
        offset: int = 0,
        kind_filter: str = "",
    ) -> tuple[list[dict], int]:
        return await search_songs(
            query=query, sort=sort, limit=limit, offset=offset, kind_filter=kind_filter
        )

    @property
    def songs(self) -> int:
        """Cached total song count (updated after each scan)."""
        return self._song_count

    # ── File watcher ──────────────────────────────────────────────────────────

    def start_watcher(self):
        handler = _DebounceHandler(self._schedule_rescan)
        self._observer = Observer()
        if settings.media_dir.exists():
            self._observer.schedule(handler, str(settings.media_dir), recursive=True)
        self._observer.start()
        log.info("File watcher started")

    def stop_watcher(self):
        if self._debounce_timer:
            self._debounce_timer.cancel()
        if self._observer:
            self._observer.stop()
            self._observer.join()

    def _schedule_rescan(self):
        """Called from watchdog thread — debounced, dispatches to event loop."""
        if self._debounce_timer:
            self._debounce_timer.cancel()
        self._debounce_timer = threading.Timer(2.0, self._fire_rescan)
        self._debounce_timer.start()

    def _fire_rescan(self):
        if self._loop:
            asyncio.run_coroutine_threadsafe(self.scan(), self._loop)


class _DebounceHandler(FileSystemEventHandler):
    def __init__(self, callback):
        self._cb = callback

    def on_created(self, event: FileSystemEvent):  self._cb()
    def on_deleted(self, event: FileSystemEvent):  self._cb()
    def on_moved(self,   event: FileSystemEvent):  self._cb()


library = Library()
