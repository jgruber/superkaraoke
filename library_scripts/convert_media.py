#!/usr/bin/env python3
"""
convert_media.py — Batch-convert karaoke media files to browser-native MP4.

Supported conversions
─────────────────────
  video formats   AVI, MKV, MOV, WMV, FLV, M4V, MPG/MPEG, TS, VOB, and any
                  other video format that ffmpeg can decode.
                  → Re-encoded as H.264/AAC MP4 with -movflags +faststart for
                    direct range-request serving (no runtime transcoding).

  cdg             CDG+MP3 karaoke pairs (files where kind='cdg' in the DB).
                  → Both files merged into a single H.264/AAC MP4.
                    The database record is updated: kind changes to 'video',
                    cdg_path is cleared, and both source files are removed.

All output files use -movflags +faststart so the moov atom sits at the front
of the file, enabling instant seeking and efficient byte-range serving.

Safe to re-run: if the target .mp4 already exists the transcode step is
skipped and only the database record is updated.

Usage
─────
  # Convert everything (default)
  python3 convert_media.py

  # Convert only AVI and MKV files
  python3 convert_media.py --types avi mkv

  # Convert only CDG+MP3 pairs
  python3 convert_media.py --types cdg

  # Convert all video files (no CDG)
  python3 convert_media.py --types video

  # Preview without making changes
  python3 convert_media.py --dry-run

  # Override paths
  python3 convert_media.py --media-dir /media/karaoke --db /data/superkaraoke.db
"""

import argparse
import hashlib
import logging
import sqlite3
import subprocess
import sys
from pathlib import Path

# ── Supported types ───────────────────────────────────────────────────────────

# All video extensions the script will consider when --types is not specified.
# These must all be formats ffmpeg can decode.
VIDEO_EXTS = {
    "avi", "mkv", "mov", "wmv", "flv", "m4v",
    "mpg", "mpeg", "ts", "vob", "divx", "ogv",
}

# "video" is an alias for all VIDEO_EXTS; "cdg" handles CDG+MP3 pairs.
# "all" means VIDEO_EXTS + cdg.
ALL_TYPES = VIDEO_EXTS | {"cdg"}

# ── Defaults ──────────────────────────────────────────────────────────────────

_DEFAULT_DB    = Path("/data/superkaraoke.db")
_DEFAULT_MEDIA = Path("/media/karaoke")

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def song_id(path: Path, media_dir: Path) -> str:
    """Stable 12-char song ID: sha256 of path relative to media_dir."""
    rel = str(path.relative_to(media_dir))
    return hashlib.sha256(rel.encode()).hexdigest()[:12]


def probe_duration(path: Path) -> float | None:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1",
             str(path)],
            capture_output=True, text=True, timeout=30,
        )
        v = r.stdout.strip()
        return round(float(v), 2) if v else None
    except Exception:
        return None


def _run_ffmpeg(cmd: list[str], mp4: Path) -> bool:
    """
    Run an ffmpeg command that writes to a temp file beside mp4.
    On success, renames the temp file to mp4.  Returns True on success.
    """
    tmp = mp4.with_suffix(".converting.mp4")
    tmp.unlink(missing_ok=True)

    result = subprocess.run(cmd + [str(tmp)],
                            capture_output=True, text=True, timeout=7200)

    if result.returncode != 0:
        log.error("  ffmpeg failed (exit %d):\n%s",
                  result.returncode, result.stderr[-3000:])
        tmp.unlink(missing_ok=True)
        return False

    if result.stderr.strip():
        for line in result.stderr.strip().splitlines()[-5:]:
            log.warning("  ffmpeg: %s", line)

    tmp.rename(mp4)
    return True


def transcode_video(src: Path, mp4: Path) -> bool:
    """Re-encode any video file → H.264/AAC faststart MP4."""
    return _run_ffmpeg([
        "ffmpeg", "-y",
        "-fflags", "+genpts+discardcorrupt",
        "-err_detect", "ignore_err",
        "-i", str(src),
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "192k", "-ac", "2",
        "-af", "aresample=async=1000",
        "-movflags", "+faststart",
    ], mp4)


def transcode_cdg(mp3: Path, cdg: Path, mp4: Path) -> bool:
    """Merge CDG+MP3 pair → H.264/AAC faststart MP4."""
    return _run_ffmpeg([
        "ffmpeg", "-y",
        "-i", str(mp3),
        "-i", str(cdg),
        "-map", "1:v",
        "-map", "0:a",
        "-g", "25",                          # 1-second keyframes at CDG's 25fps
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "192k",
        "-af", "silenceremove=stop_periods=-1:stop_duration=2:stop_threshold=-50dB",
        "-movflags", "+faststart",
    ], mp4)


def update_db(db: sqlite3.Connection, old_id: str, new_id: str,
              file_path: Path, duration: float | None,
              kind: str = "video", clear_cdg_path: bool = False) -> None:
    """Update the songs row in-place, guarding against duplicate new IDs."""
    existing = db.execute(
        "SELECT id FROM songs WHERE id = ? AND id != ?", (new_id, old_id)
    ).fetchone()
    if existing:
        log.warning("  new id %s already in DB — removing old row only", new_id)
        db.execute("DELETE FROM songs WHERE id = ?", (old_id,))
    else:
        fields = "id = ?, file_path = ?, kind = ?, duration_secs = ?"
        params: list = [new_id, str(file_path), kind, duration]
        if clear_cdg_path:
            fields += ", cdg_path = NULL"
        db.execute(f"UPDATE songs SET {fields} WHERE id = ?", params + [old_id])
    db.commit()


# ── Conversion routines ───────────────────────────────────────────────────────

def convert_video_rows(rows, media_dir: Path, db: sqlite3.Connection,
                       dry_run: bool) -> tuple[int, int, int, int]:
    converted = skipped = failed = missing = 0

    for row in rows:
        src    = Path(row["file_path"])
        mp4    = src.with_suffix(".mp4")
        old_id = row["id"]
        ext    = src.suffix.lstrip(".").lower()

        try:
            new_id = song_id(mp4, media_dir)
        except ValueError:
            log.warning("  Cannot compute ID (not under media-dir): %s", src)
            skipped += 1
            continue

        log.info("[%s] %s  (%s)", old_id, src.name, ext.upper())

        if not src.exists():
            log.warning("  Source not found on disk — skipping")
            missing += 1
            continue

        if dry_run:
            log.info("  → would transcode to %s (new id: %s)", mp4.name, new_id)
            continue

        if mp4.exists():
            log.info("  MP4 already exists — skipping transcode")
        else:
            log.info("  Transcoding → %s", mp4.name)
            if not transcode_video(src, mp4):
                log.error("  FAILED — source kept, database unchanged")
                failed += 1
                continue

        duration = probe_duration(mp4)
        update_db(db, old_id, new_id, mp4, duration)

        try:
            src.unlink()
        except OSError as e:
            log.warning("  Could not remove source: %s", e)

        log.info("  ✓ done (id %s → %s, duration %.1fs)",
                 old_id, new_id, duration or 0)
        converted += 1

    return converted, skipped, failed, missing


def convert_cdg_rows(rows, media_dir: Path, db: sqlite3.Connection,
                     dry_run: bool) -> tuple[int, int, int, int]:
    converted = skipped = failed = missing = 0

    for row in rows:
        mp3    = Path(row["file_path"])
        cdg    = Path(row["cdg_path"])
        mp4    = mp3.with_suffix(".mp4")
        old_id = row["id"]

        try:
            new_id = song_id(mp4, media_dir)
        except ValueError:
            log.warning("  Cannot compute ID (not under media-dir): %s", mp3)
            skipped += 1
            continue

        log.info("[%s] %s  (CDG+MP3)", old_id, mp3.stem)

        if not mp3.exists() or not cdg.exists():
            log.warning("  Source file(s) not found on disk — skipping")
            missing += 1
            continue

        if dry_run:
            log.info("  → would merge CDG+MP3 to %s (new id: %s)", mp4.name, new_id)
            continue

        if mp4.exists():
            log.info("  MP4 already exists — skipping transcode")
        else:
            log.info("  Merging CDG+MP3 → %s", mp4.name)
            if not transcode_cdg(mp3, cdg, mp4):
                log.error("  FAILED — sources kept, database unchanged")
                failed += 1
                continue

        duration = probe_duration(mp4)
        update_db(db, old_id, new_id, mp4, duration,
                  kind="video", clear_cdg_path=True)

        for f in (mp3, cdg):
            try:
                f.unlink()
            except OSError as e:
                log.warning("  Could not remove %s: %s", f.name, e)

        log.info("  ✓ done (id %s → %s, duration %.1fs)",
                 old_id, new_id, duration or 0)
        converted += 1

    return converted, skipped, failed, missing


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--types", nargs="*", metavar="TYPE",
        help=(
            "File types to convert. Any mix of: "
            + ", ".join(sorted(VIDEO_EXTS))
            + ", cdg, video (all video formats), all (default: all types). "
            "Example: --types avi mkv cdg"
        ),
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without modifying anything")
    parser.add_argument("--media-dir", type=Path, default=_DEFAULT_MEDIA,
                        help=f"Media root directory (default: {_DEFAULT_MEDIA})")
    parser.add_argument("--db", type=Path, default=_DEFAULT_DB,
                        help=f"Path to superkaraoke.db (default: {_DEFAULT_DB})")
    args = parser.parse_args()

    # ── Resolve requested types ───────────────────────────────────────────────
    if not args.types:
        requested = ALL_TYPES
    else:
        requested: set[str] = set()
        for t in args.types:
            t = t.lower().lstrip(".")
            if t in ("all",):
                requested = ALL_TYPES
                break
            elif t == "video":
                requested |= VIDEO_EXTS
            elif t == "cdg":
                requested.add("cdg")
            elif t in VIDEO_EXTS:
                requested.add(t)
            else:
                log.warning("Unknown type %r — ignored (known types: %s)",
                            t, ", ".join(sorted(ALL_TYPES)))

    do_cdg   = "cdg" in requested
    do_video = requested - {"cdg"}

    if not do_cdg and not do_video:
        log.error("No valid types selected.")
        sys.exit(1)

    if not args.db.exists():
        log.error("Database not found: %s", args.db)
        sys.exit(1)

    db = sqlite3.connect(str(args.db))
    db.row_factory = sqlite3.Row

    total_converted = total_skipped = total_failed = total_missing = 0

    # ── Video conversions ─────────────────────────────────────────────────────
    if do_video:
        # Build a LIKE clause for each extension
        like_clauses = " OR ".join(
            "LOWER(file_path) LIKE ?" for _ in do_video
        )
        params = [f"%.{ext}" for ext in sorted(do_video)]
        video_rows = db.execute(
            f"SELECT * FROM songs WHERE kind = 'video' AND ({like_clauses})",
            params,
        ).fetchall()

        if video_rows:
            log.info("─── Video files: %d song(s) to convert %s",
                     len(video_rows),
                     "(DRY RUN)" if args.dry_run else "")
            c, s, f, m = convert_video_rows(video_rows, args.media_dir, db, args.dry_run)
            total_converted += c
            total_skipped   += s
            total_failed    += f
            total_missing   += m
        else:
            exts = ", ".join(sorted(do_video))
            log.info("No video songs found for types: %s", exts)

    # ── CDG conversions ───────────────────────────────────────────────────────
    if do_cdg:
        cdg_rows = db.execute(
            "SELECT * FROM songs WHERE kind = 'cdg'"
        ).fetchall()

        if cdg_rows:
            log.info("─── CDG+MP3 pairs: %d song(s) to convert %s",
                     len(cdg_rows),
                     "(DRY RUN)" if args.dry_run else "")
            c, s, f, m = convert_cdg_rows(cdg_rows, args.media_dir, db, args.dry_run)
            total_converted += c
            total_skipped   += s
            total_failed    += f
            total_missing   += m
        else:
            log.info("No CDG+MP3 songs found in database.")

    db.close()
    log.info("")
    log.info("Results: %d converted, %d skipped, %d missing, %d failed",
             total_converted, total_skipped, total_missing, total_failed)


if __name__ == "__main__":
    main()
