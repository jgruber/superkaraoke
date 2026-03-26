"""
Karaoke library: syncs filesystem → SQLite on startup and on file changes.

The database is the source of truth for all song metadata.
This module owns the scan loop and file watcher.

Scan strategy (fast for large libraries):
  1. Load all existing song IDs from DB at scan start.
  2. For known files: update file paths only — no mutagen read.
  3. For new files: read tags/filename in parallel via a thread pool.
  4. Remove DB entries whose files no longer exist on disk.
"""
import asyncio
import hashlib
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers import Observer

from .config import settings
from .database import (
    upsert_song, bulk_upsert_songs, touch_song_paths, get_song, search_songs,
    count_songs, remove_missing_songs, get_all_song_ids,
)
from .metadata import extract_metadata

log = logging.getLogger(__name__)

VIDEO_EXTS = set(settings.supported_video_extensions)
AUDIO_EXTS = set(settings.supported_audio_extensions)
CDG_EXTS   = set(settings.supported_cdg_extensions)

# Thread pool for parallel mutagen reads on first-scan new files
_SCAN_WORKERS = min(16, (os.cpu_count() or 4) * 2)
_executor = ThreadPoolExecutor(max_workers=_SCAN_WORKERS, thread_name_prefix="scan")


def _song_id(path: Path) -> str:
    """Stable 12-char ID: sha256 of path relative to media_dir."""
    rel = str(path.relative_to(settings.media_dir))
    return hashlib.sha256(rel.encode()).hexdigest()[:12]


class Library:
    def __init__(self):
        self._song_count: int = 0
        self._scanning: bool = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._observer: Optional[Observer] = None
        self._debounce_timer: Optional[threading.Timer] = None

    # ── Scanning ──────────────────────────────────────────────────────────────

    async def scan(self):
        if self._scanning:
            log.debug("Scan already in progress, skipping")
            return
        self._scanning = True
        self._loop = asyncio.get_running_loop()
        try:
            await self._do_scan()
        finally:
            self._scanning = False

    async def _do_scan(self):
        media_dir = settings.media_dir
        if not media_dir.exists():
            log.warning("Media dir does not exist: %s", media_dir)
            self._song_count = 0
            return

        # Snapshot of what's already indexed — used to skip mutagen reads
        existing_ids = await get_all_song_ids()
        found_ids: set[str] = set()

        # Walk filesystem, grouping files by directory for CDG pairing
        by_dir: dict[Path, list[Path]] = {}
        for dirpath, _dirnames, filenames in os.walk(
            media_dir,
            onerror=lambda e: log.debug("Skipping inaccessible path: %s", e),
        ):
            for name in filenames:
                by_dir.setdefault(Path(dirpath), []).append(Path(dirpath) / name)

        log.info("Scan started: %d directories, %d existing DB entries",
                 len(by_dir), len(existing_ids))

        # Separate files into known (fast path) and new (need metadata read)
        known:    list[tuple[str, str, Optional[str], str]] = []  # (sid, file, cdg, kind)
        new_files: list[tuple[str, str, Optional[str], str]] = []

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
                    entry = (sid, str(f), str(cdg), "cdg")

                elif ext in VIDEO_EXTS:
                    sid = _song_id(f)
                    entry = (sid, str(f), None, "video")

                else:
                    continue

                found_ids.add(entry[0])
                if entry[0] in existing_ids:
                    known.append(entry)
                else:
                    new_files.append(entry)

        log.info("Scan: %d known (path update only), %d new (reading metadata)",
                 len(known), len(new_files))

        # Fast path: bulk-update file paths for known songs
        for sid, file_path, cdg_path, kind in known:
            await touch_song_paths(sid, file_path, cdg_path, kind)

        # Slow path: read metadata for new files in parallel, bulk-insert in batches
        if new_files:
            loop = asyncio.get_running_loop()
            chunk = 500

            for i in range(0, len(new_files), chunk):
                batch = new_files[i:i + chunk]

                # Parallel metadata reads via thread pool
                # CDG files: skip mutagen (filename parsing only — avoids slow NAS reads)
                # Video files: read embedded tags (filenames are less structured)
                metas = await asyncio.gather(*[
                    loop.run_in_executor(
                        _executor, extract_metadata, fp, cdg, kind,
                        kind == "video",   # read_tags
                    )
                    for _, fp, cdg, kind in batch
                ])

                songs_batch = [
                    {"id": sid, "file_path": fp, "cdg_path": cdg, "kind": kind, **meta}
                    for (sid, fp, cdg, kind), meta in zip(batch, metas)
                ]

                # Single transaction for the whole chunk
                await bulk_upsert_songs(songs_batch)
                log.info("Scan progress: %d / %d new files",
                         min(i + chunk, len(new_files)), len(new_files))

        await remove_missing_songs(found_ids)
        self._song_count = await count_songs()
        log.info("Scan complete: %d songs", self._song_count)

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
            query=query, sort=sort, limit=limit, offset=offset,
            kind_filter=kind_filter, include_duplicates=False,
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
