#!/usr/bin/env python3
"""
path_replace.py — Replace a path prefix in the song library database.

Use this to migrate a database between machines or containers where the media
directory is mounted at a different location.  All file_path and cdg_path
values that start with OLD_PREFIX are updated to start with NEW_PREFIX, and
song IDs are recomputed from the new paths.

Song IDs are sha256 of the path relative to the media directory (first 12 hex
characters).  If the prefix change does not affect the relative portion of the
path — the common case when only the mount point changes — IDs are unchanged.
If the relative path changes (e.g. the media-dir itself is being renamed), the
new ID is derived from the path relative to --media-dir (the new media root).

Usage
─────
  # Preview what would change (no database is modified)
  python3 library_scripts/path_replace.py /mnt/nas/karaoke /media/karaoke --dry-run

  # Apply the substitution
  python3 library_scripts/path_replace.py /mnt/nas/karaoke /media/karaoke

  # Specify a non-default database or media directory
  python3 library_scripts/path_replace.py /old/path /new/path \\
      --db /data/superkaraoke.db \\
      --media-dir /media/karaoke

  # Trailing slashes are normalised — these are equivalent:
  python3 library_scripts/path_replace.py /mnt/nas/karaoke/ /media/karaoke/
  python3 library_scripts/path_replace.py /mnt/nas/karaoke  /media/karaoke
"""

import argparse
import hashlib
import logging
import sqlite3
import sys
from pathlib import Path

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

def _song_id(file_path: str, media_dir: Path) -> str:
    """Recompute the 12-char song ID from the new file path."""
    rel = str(Path(file_path).relative_to(media_dir))
    return hashlib.sha256(rel.encode()).hexdigest()[:12]


def _apply_prefix(path: str, old: str, new: str) -> str | None:
    """Return the substituted path if it starts with old, else None."""
    if path.startswith(old):
        return new + path[len(old):]
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("old_prefix",
                        help="Path prefix to replace (e.g. /mnt/nas/karaoke)")
    parser.add_argument("new_prefix",
                        help="Replacement prefix (e.g. /media/karaoke)")
    parser.add_argument("--db", type=Path, default=_DEFAULT_DB,
                        help=f"Path to superkaraoke.db (default: {_DEFAULT_DB})")
    parser.add_argument("--media-dir", type=Path, default=_DEFAULT_MEDIA,
                        help=f"New media root, used to recompute song IDs "
                             f"(default: {_DEFAULT_MEDIA})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without modifying the database")
    args = parser.parse_args()

    # Normalise: strip trailing slashes so prefix matching is unambiguous
    old = args.old_prefix.rstrip("/")
    new = args.new_prefix.rstrip("/")

    if old == new:
        log.error("old_prefix and new_prefix are the same — nothing to do.")
        sys.exit(1)

    if not args.db.exists():
        log.error("Database not found: %s", args.db)
        sys.exit(1)

    db = sqlite3.connect(str(args.db))
    db.row_factory = sqlite3.Row

    rows = db.execute(
        "SELECT id, file_path, cdg_path FROM songs ORDER BY file_path"
    ).fetchall()

    total = changed = skipped = errors = 0

    for row in rows:
        total += 1
        old_id   = row["id"]
        old_fp   = row["file_path"] or ""
        old_cp   = row["cdg_path"]  or ""

        new_fp = _apply_prefix(old_fp, old, new)
        if new_fp is None:
            # file_path doesn't start with old_prefix — skip
            continue

        new_cp = _apply_prefix(old_cp, old, new) if old_cp else None

        # Recompute ID from the new file_path relative to the new media_dir
        try:
            new_id = _song_id(new_fp, args.media_dir)
        except ValueError:
            log.warning(
                "  [SKIP] %s — new path %r is not under --media-dir %s",
                old_id, new_fp, args.media_dir,
            )
            skipped += 1
            continue

        id_changed = new_id != old_id
        fp_label   = f"{old_fp!r}  →  {new_fp!r}"
        id_label   = f"  (id {old_id} → {new_id})" if id_changed else ""

        if args.dry_run:
            print(f"  WOULD UPDATE  {fp_label}{id_label}")
            if new_cp and new_cp != old_cp:
                print(f"                cdg: {old_cp!r}  →  {new_cp!r}")
            changed += 1
            continue

        # Guard against a collision where new_id already belongs to a different row
        if id_changed:
            conflict = db.execute(
                "SELECT id FROM songs WHERE id = ? AND id != ?", (new_id, old_id)
            ).fetchone()
            if conflict:
                log.warning(
                    "  [SKIP] %s — target id %s already exists in DB (collision); "
                    "run a full library rescan after this script to resolve.",
                    old_id, new_id,
                )
                skipped += 1
                errors += 1
                continue

        db.execute(
            """
            UPDATE songs
               SET id        = ?,
                   file_path = ?,
                   cdg_path  = ?
             WHERE id = ?
            """,
            (
                new_id,
                new_fp,
                new_cp if new_cp else (old_cp or None),
                old_id,
            ),
        )
        log.info("  UPDATED  %s%s", fp_label, id_label)
        changed += 1

    if not args.dry_run:
        db.commit()

    db.close()

    print()
    label = "DRY RUN — would update" if args.dry_run else "Updated"
    print(f"{label}: {changed}  Skipped: {skipped}  (of {total} total songs)")
    if errors:
        print(f"Errors (ID collisions): {errors} — run a library rescan to resolve.")


if __name__ == "__main__":
    main()
