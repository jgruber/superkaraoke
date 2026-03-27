#!/usr/bin/env python3
"""
mb_fix.py — Enrich track metadata (title, artist, year, genre) via MusicBrainz.

By default every song in the library is processed.  Use --no-artist to restrict
to only songs that currently have no artist set.

When a match is applied the script:
  • Renames the file(s) to "Artist - Title.ext" (library-scanner format).
  • Updates the database: title, artist, year, genre, file_path, cdg_path,
    id, and metadata_locked=1.

Usage
─────
  # Interactive (default) — process all songs, review and pick each match
  python3 mb_fix.py

  # Only songs with no artist set
  python3 mb_fix.py --no-artist

  # Automatic — apply only score=100 matches (default); when tied, picks lowest year
  python3 mb_fix.py --auto

  # Looser automatic (accept scores >= 90)
  python3 mb_fix.py --auto --min-score 90

  # Delete files with no match instead of skipping them
  python3 mb_fix.py --auto --delete-unmatched

  # Only process files whose name ends with a YouTube video ID
  python3 mb_fix.py --youtube-only

  # Preview without touching files or database
  python3 mb_fix.py --auto --dry-run

  # Process at most N songs then stop
  python3 mb_fix.py --limit 100

  # Skip the first N songs (resume from where you left off)
  python3 mb_fix.py --offset 200

  # Override paths
  python3 mb_fix.py --db superkaraoke.db --media-dir /media/karaoke
"""

import argparse
import hashlib
import logging
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

# ── Defaults ──────────────────────────────────────────────────────────────────

_DEFAULT_DB    = Path("/data/superkaraoke.db")
_DEFAULT_MEDIA = Path("/media/karaoke")

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ── Terminal colours (degrade gracefully if not a tty) ────────────────────────

_COLOUR = sys.stdout.isatty()

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOUR else text

def bold(s):    return _c("1", s)
def dim(s):     return _c("2", s)
def green(s):   return _c("32", s)
def yellow(s):  return _c("33", s)
def cyan(s):    return _c("36", s)
def red(s):     return _c("31", s)


def score_colour(n: int) -> str:
    if n >= 90:  return green(f"{n:3d}")
    if n >= 70:  return yellow(f"{n:3d}")
    return red(f"{n:3d}")


# ── MusicBrainz rate-limited search ───────────────────────────────────────────

_MB_API     = "https://musicbrainz.org/ws/2/recording/"
_MB_HEADERS = {"User-Agent": "SuperKaraoke/1.0 (https://github.com/superkaraoke)"}
_MB_RATE    = 1.1   # seconds between calls
_last_mb: float = 0.0


def search_mb(title: str, artist: str = "", limit: int = 8) -> list[dict]:
    """Synchronous MusicBrainz search, rate-limited."""
    import json
    import urllib.parse
    import urllib.request

    global _last_mb
    wait = _MB_RATE - (time.monotonic() - _last_mb)
    if wait > 0:
        time.sleep(wait)
    _last_mb = time.monotonic()

    parts = []
    if title:
        parts.append(f'recording:"{title}"')
    if artist:
        parts.append(f'artist:"{artist}"')
    query = " AND ".join(parts) if parts else title

    url = _MB_API + "?" + urllib.parse.urlencode({
        "query": query, "fmt": "json", "limit": str(limit)
    })
    req = urllib.request.Request(url, headers=_MB_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:
        print(f"  {red('MusicBrainz error:')} {exc}")
        return []

    results = []
    for rec in data.get("recordings", []):
        credits = rec.get("artist-credit", [])
        mb_artist = " & ".join(
            c.get("name") or c.get("artist", {}).get("name", "")
            for c in credits if isinstance(c, dict)
        ).strip()

        date_str = rec.get("first-release-date", "")
        year = int(date_str[:4]) if len(date_str) >= 4 and date_str[:4].isdigit() else None

        raw_tags = sorted(rec.get("tags", []), key=lambda t: -t.get("count", 0))
        genre = raw_tags[0]["name"].title() if raw_tags else ""

        results.append({
            "title":  rec.get("title", ""),
            "artist": mb_artist,
            "year":   year,
            "genre":  genre,
            "score":  rec.get("score", 0),
        })

    return results


def search_mb_with_fallback(
    title_q: str, artist_hint: str
) -> tuple[list[dict], bool]:
    """
    Search MusicBrainz with (title_q, artist_hint).  If no results are returned
    and artist_hint is non-empty, retry with the arguments swapped
    (artist_hint as the recording title, title_q as the artist).

    Returns (candidates, reversed) where reversed=True means the swap was used.
    """
    candidates = search_mb(title_q, artist_hint)
    if candidates or not artist_hint:
        return candidates, False
    swapped = search_mb(artist_hint, title_q)
    return swapped, bool(swapped)


# ── Query construction ────────────────────────────────────────────────────────

# Karaoke-specific noise to strip before searching
_KARAOKE_NOISE = re.compile(
    r'\s*(w\s*vocals?|wvocals?|with\s+vocals?|backing\s+track|'
    r'no\s+vocal|instrumental|karaoke|duet)\s*$',
    re.IGNORECASE,
)
# YouTube video ID: 11 base64url chars preceded by a space/underscore or in brackets
# e.g. "Never Gonna Give You Up_dQw4w9WgXcQ" or "Title [dQw4w9WgXcQ]"
_YOUTUBE_ID = re.compile(r'[\s_]\[?[A-Za-z0-9_-]{11}\]?\s*$')
# Directory names that are definitely NOT artist names
_GENERIC_DIRS = re.compile(
    r'^(karaoke|2009|2010|2011|2012|2013|2014|2015|2016|2017|2018|2019|2020|'
    r'2021|2022|2023|2024|artists?|songs?|albums?|music|downloads?|downloaded|'
    r'best\s+of|collection|misc|various|vario|mixed|new|vol|volume|\d+)$',
    re.IGNORECASE,
)


# Leading catalog+track prefix: e.g. "DK041-14-", "LEG091-08-", "SF135-10-"
_CATALOG_PREFIX = re.compile(r'^[A-Za-z]*\d+[-_]\d+[-_]')

# Karaoke "in the style of Artist" convention — with or without surrounding parens:
#   "Applause in the style of Lady Gaga"
#   "Applause (in the style of Lady Gaga)"
_STYLE_OF = re.compile(
    r'^(.+?)\s+\(?\s*in\s+the\s+style\s+of\s+(.+?)\s*\)?\s*$',
    re.IGNORECASE,
)


def _clean_title(s: str) -> str:
    """Strip karaoke noise, YouTube IDs, and catalog/track prefixes; replace underscores."""
    s = _YOUTUBE_ID.sub('', s).strip()   # strip before underscore→space so ID boundary is clear
    s = s.replace('_', ' ')
    s = _CATALOG_PREFIX.sub('', s).strip()
    s = _KARAOKE_NOISE.sub('', s).strip()
    return s


def build_query(row: sqlite3.Row) -> tuple[str, str]:
    """
    Return (title_query, artist_hint) derived from the DB row.

    title_query  — the recording name to search for (cleaned)
    artist_hint  — optional artist to narrow the search (may be empty)
    """
    db_title = (row["title"] or "").strip()
    fp       = row["file_path"] or ""
    parts    = Path(fp).parts   # e.g. ('/', 'media', 'karaoke', '2009', ..., 'Artist Name', 'file.mp3')

    title = _clean_title(db_title) or Path(fp).stem

    # Heuristic: walk directory parts from deepest-1 upward, looking for a
    # plausible artist name (not generic, not the root media dir).
    dir_hint = ""
    for part in reversed(parts[:-1]):          # skip the filename itself
        if not _GENERIC_DIRS.match(part):
            # Use this directory as artist hint if it has word-like content
            if re.search(r'[A-Za-z]{3,}', part):
                dir_hint = part
                break

    artist_hint = dir_hint

    if " - " in title:
        # Split on " - " separators.  If the first segment is a catalog/track
        # code (alphanumeric + optional dash+digits, no spaces — e.g. "pi326-05",
        # "DK041-14", "LEG091") drop it and read the rest as artist - title.
        segs = [s.strip() for s in title.split(" - ")]
        if re.match(r'^[A-Za-z]+\d+(-\d+)?$', segs[0]) and len(segs) >= 3:
            # catalog - artist - title
            artist_hint = segs[1]
            title       = " - ".join(segs[2:])
        elif len(segs) >= 2:
            # artist - title  (the normal case where parse_filename missed it)
            artist_hint = segs[0]
            title       = " - ".join(segs[1:])
    elif "-" in title:
        # Detect embedded "Title-Artist" (bare hyphen, no spaces) pattern:
        # e.g. "Jack & Diane-John Cougar Mellencamp"
        idx = title.rfind("-")
        possible_title  = title[:idx].strip()
        possible_artist = title[idx+1:].strip().replace('_', ' ')
        if possible_title and re.search(r'[A-Za-z]{2,}', possible_artist):
            title       = possible_title
            artist_hint = possible_artist

    # "Song Title in the style of Artist Name" — karaoke convention.
    # Always overrides the directory hint: an explicit attribution beats a guessed
    # directory name (e.g. "Sing King" is a karaoke channel, not the original artist).
    m = _STYLE_OF.match(title)
    if m:
        title       = m.group(1).strip()
        artist_hint = m.group(2).strip()

    return title, artist_hint


# ── Auto-mode candidate selection ────────────────────────────────────────────

def pick_best_auto(candidates: list[dict], min_score: int) -> Optional[dict]:
    """
    Return the best candidate for automatic application, or None.

    Rules:
      1. Only candidates whose score == the highest score (and >= min_score)
         are considered.  So if min_score=100, only perfect-100 results qualify.
      2. Among ties at the top score, prefer the entry with the lowest release
         year (entries with no year sort last).
    """
    eligible = [c for c in candidates if c["score"] >= min_score]
    if not eligible:
        return None
    top_score = max(c["score"] for c in eligible)
    top = [c for c in eligible if c["score"] == top_score]
    # Sort: year present first (ascending), then no-year entries
    top.sort(key=lambda c: (c["year"] is None, c["year"] or 0))
    return top[0]


# ── Song ID (must match server/database.py) ───────────────────────────────────

def song_id(path: Path, media_dir: Path) -> str:
    rel = str(path.relative_to(media_dir))
    return hashlib.sha256(rel.encode()).hexdigest()[:12]


# ── Filename helpers ──────────────────────────────────────────────────────────

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

def safe_filename(s: str) -> str:
    return _UNSAFE_CHARS.sub('', s).strip().rstrip('.')


def make_new_stem(artist: str, title: str) -> str:
    return safe_filename(f"{artist} - {title}")


# ── Database helpers ──────────────────────────────────────────────────────────

def get_songs(
    db: sqlite3.Connection,
    offset: int,
    limit: Optional[int],
    no_artist_only: bool = False,
) -> list[sqlite3.Row]:
    where = "WHERE (artist = '' OR artist IS NULL)" if no_artist_only else ""
    sql = f"""
        SELECT id, file_path, cdg_path, title, artist, kind, metadata_locked
        FROM songs
        {where}
        ORDER BY file_path
    """
    if limit:
        sql += f" LIMIT {limit} OFFSET {offset}"
    elif offset:
        sql += f" LIMIT -1 OFFSET {offset}"
    return db.execute(sql).fetchall()


def delete_song(
    db:        sqlite3.Connection,
    row:       sqlite3.Row,
    dry_run:   bool,
) -> bool:
    """Delete file(s) from disk and remove the DB entry. Returns True on success."""
    fp = Path(row["file_path"])
    cp = Path(row["cdg_path"]) if row["cdg_path"] else None

    if dry_run:
        print(f"  {dim('DRY RUN')} would delete {fp}")
        if cp:
            print(f"  {dim('DRY RUN')} would delete {cp}")
        print(f"  {dim('DRY RUN')} would remove DB entry id={row['id']}")
        return True

    ok = True
    for path in ([fp] + ([cp] if cp else [])):
        if path.exists():
            try:
                path.unlink()
                print(f"  {red('Deleted:')} {path}")
            except OSError as e:
                print(f"  {red('Delete failed:')} {e}")
                ok = False
        else:
            print(f"  {yellow('Not found (skipping):')} {path}")

    db.execute("DELETE FROM songs WHERE id = ?", (row["id"],))
    db.commit()
    return ok


def apply_match(
    db:        sqlite3.Connection,
    row:       sqlite3.Row,
    candidate: dict,
    media_dir: Path,
    dry_run:   bool,
) -> bool:
    """Rename file(s) and update the database. Returns True on success."""
    old_id   = row["id"]
    fp       = Path(row["file_path"])
    cp       = Path(row["cdg_path"]) if row["cdg_path"] else None
    ext      = fp.suffix.lower()

    new_title  = candidate["title"]
    new_artist = candidate["artist"]
    new_stem   = make_new_stem(new_artist, new_title)
    new_fp     = fp.parent / f"{new_stem}{ext}"
    new_cp     = (cp.parent / f"{new_stem}.cdg") if cp else None

    try:
        new_id = song_id(new_fp, media_dir)
    except ValueError:
        print(f"  {red('Error:')} new path {new_fp} is not under media-dir")
        return False

    if dry_run:
        print(f"  {dim('DRY RUN')} would rename → {bold(new_fp.name)}")
        if new_cp:
            print(f"  {dim('DRY RUN')} would rename → {bold(new_cp.name)}")
        print(f"  {dim('DRY RUN')} would update DB: title={new_title!r}, artist={new_artist!r}, "
              f"year={candidate.get('year')}, genre={candidate.get('genre')!r}")
        return True

    # Rename audio file
    if fp != new_fp:
        if not fp.exists():
            print(f"  {red('Error:')} source not found: {fp}")
            return False
        try:
            fp.rename(new_fp)
        except OSError as e:
            print(f"  {red('Rename failed:')} {e}")
            return False

    # Rename CDG file
    if cp and new_cp and cp != new_cp:
        if cp.exists():
            try:
                cp.rename(new_cp)
            except OSError as e:
                print(f"  {yellow('CDG rename failed:')} {e}")
        else:
            print(f"  {yellow('CDG not found:')} {cp}")

    # Update database
    existing = db.execute(
        "SELECT id FROM songs WHERE id = ? AND id != ?", (new_id, old_id)
    ).fetchone()
    if existing:
        db.execute("DELETE FROM songs WHERE id = ?", (old_id,))
    else:
        db.execute("""
            UPDATE songs
               SET id               = ?,
                   file_path        = ?,
                   cdg_path         = ?,
                   title            = ?,
                   artist           = ?,
                   year             = ?,
                   genre            = ?,
                   metadata_locked  = 1,
                   metadata_updated = 1
             WHERE id = ?
        """, (
            new_id,
            str(new_fp),
            str(new_cp) if new_cp else None,
            new_title,
            new_artist,
            candidate.get("year"),
            candidate.get("genre") or "",
            old_id,
        ))
    db.commit()
    return True


# ── Display helpers ───────────────────────────────────────────────────────────

def print_candidates(candidates: list[dict]) -> None:
    if not candidates:
        print(f"  {red('No results found.')}")
        return
    w_title  = max(len(c["title"])  for c in candidates)
    w_artist = max(len(c["artist"]) for c in candidates)
    w_title  = max(w_title,  5)
    w_artist = max(w_artist, 6)
    print()
    hdr = (f"  {'#':>2}  {bold('Score')}  "
           f"{'Title':<{w_title}}  {'Artist':<{w_artist}}  Year  Genre")
    print(hdr)
    print(f"  {'─'*2}  {'─'*5}  {'─'*w_title}  {'─'*w_artist}  {'─'*4}  {'─'*10}")
    for i, c in enumerate(candidates, 1):
        year  = str(c["year"]) if c["year"] else "    "
        genre = (c["genre"] or "")[:20]
        print(f"  {i:>2}  {score_colour(c['score'])}  "
              f"{c['title']:<{w_title}}  {c['artist']:<{w_artist}}  {year}  {genre}")
    print()


def print_song_header(row: sqlite3.Row, idx: int, total: int,
                      title_q: str, artist_hint: str) -> None:
    print()
    print("─" * 72)
    print(f"{bold(f'[{idx}/{total}]')}  {cyan(row['title'] or Path(row['file_path']).stem)}")
    print(f"  {dim('Path:')}  {row['file_path']}")
    query_str = title_q
    if artist_hint:
        query_str += f"  {dim('[artist hint: ' + artist_hint + ']')}"
    print(f"  {dim('Query:')} {query_str}")


# ── Interactive loop ──────────────────────────────────────────────────────────

def interactive_loop(
    songs:     list[sqlite3.Row],
    db:        sqlite3.Connection,
    media_dir: Path,
    dry_run:   bool,
) -> None:
    total = len(songs)
    applied = skipped = deleted = errors = 0
    auto_rest = False   # user can type 'a' to switch to auto for the rest
    auto_min_score = 100

    i = 0
    while i < total:
        row = songs[i]
        title_q, artist_hint = build_query(row)

        print_song_header(row, i + 1, total, title_q, artist_hint)

        candidates, query_reversed = search_mb_with_fallback(title_q, artist_hint)
        if query_reversed:
            print(f"  {yellow('No results — retried with title/artist swapped')}")
        print_candidates(candidates)

        if auto_rest:
            # Apply best candidate automatically (same logic as auto mode)
            best = pick_best_auto(candidates, auto_min_score) if candidates else None
            if best:
                print(f"  {green('Auto-applying:')} {best['artist']} – {best['title']}")
                if apply_match(db, row, best, media_dir, dry_run):
                    applied += 1
                else:
                    errors += 1
            else:
                print(f"  {yellow('Auto-skip:')} no result above score {auto_min_score}")
                skipped += 1
            i += 1
            continue

        while True:
            # Build prompt dynamically — number range only shown when there are results
            if candidates:
                pick = f"[1-{len(candidates)}] pick / " if len(candidates) > 1 else "[1] pick / "
            else:
                pick = ""
            prompt = f"  {pick}[m]anual / [r]efine query / [d]elete / [s]kip / [a]uto rest / [q]uit: "

            try:
                ans = input(prompt).strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                print(f"\nInterrupted. Applied: {applied}  Deleted: {deleted}  Skipped: {skipped}  Errors: {errors}")
                return

            if ans == 'q':
                print(f"\nQuit. Applied: {applied}  Deleted: {deleted}  Skipped: {skipped}  Errors: {errors}")
                return

            if ans == 's':
                skipped += 1
                i += 1
                break

            if ans == 'd':
                fp_label = row["file_path"]
                try:
                    confirm = input(f"  {red('Delete')} {fp_label}? [y/N]: ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print()
                    continue
                if confirm == 'y':
                    if delete_song(db, row, dry_run):
                        deleted += 1
                    else:
                        errors += 1
                    i += 1
                    break
                else:
                    print(f"  {dim('Delete cancelled.')}")
                    continue

            if ans == 'm':
                try:
                    man_title = input("  Title:  ").strip()
                    if not man_title:
                        print(f"  {yellow('Title is required.')}")
                        continue
                    man_artist = input("  Artist: ").strip()
                    if not man_artist:
                        print(f"  {yellow('Artist is required.')}")
                        continue
                except (EOFError, KeyboardInterrupt):
                    print()
                    continue
                manual = {"title": man_title, "artist": man_artist, "year": None, "genre": ""}
                print(f"  {green('Applying:')} {man_artist} – {man_title}")
                if apply_match(db, row, manual, media_dir, dry_run):
                    applied += 1
                else:
                    errors += 1
                i += 1
                break

            if ans == 'a':
                try:
                    min_s = input(f"  Min score for auto (default {auto_min_score}): ").strip()
                    if min_s.isdigit():
                        auto_min_score = int(min_s)
                except (EOFError, KeyboardInterrupt):
                    pass
                auto_rest = True
                break   # re-enter the while loop to process this song in auto mode

            if ans == 'r':
                custom = input("  Enter search query (or 'title | artist'): ").strip()
                if not custom:
                    continue
                if '|' in custom:
                    parts = custom.split('|', 1)
                    t_q = parts[0].strip()
                    a_h = parts[1].strip()
                else:
                    t_q = custom
                    a_h = ""
                candidates = search_mb(t_q, a_h)
                print_candidates(candidates)
                continue

            if ans.isdigit():
                n = int(ans)
                if 1 <= n <= len(candidates):
                    c = candidates[n - 1]
                    print(f"  {green('Applying:')} {c['artist']} – {c['title']}  "
                          f"({c.get('year', '')}  {c.get('genre', '')})")
                    if apply_match(db, row, c, media_dir, dry_run):
                        applied += 1
                    else:
                        errors += 1
                    i += 1
                    break
                else:
                    print(f"  Enter a number between 1 and {len(candidates)}")
                    continue

    print()
    print(f"Done. Applied: {applied}  Deleted: {deleted}  Skipped: {skipped}  Errors: {errors}")


# ── Automatic loop ────────────────────────────────────────────────────────────

def auto_loop(
    songs:          list[sqlite3.Row],
    db:             sqlite3.Connection,
    media_dir:      Path,
    dry_run:        bool,
    min_score:      int,
    delete_unmatched: bool = False,
) -> None:
    total = len(songs)
    applied = skipped = deleted = errors = 0

    for i, row in enumerate(songs, 1):
        title_q, artist_hint = build_query(row)
        candidates, query_reversed = search_mb_with_fallback(title_q, artist_hint)

        title_label = row["title"] or Path(row["file_path"]).stem
        reversed_note = " (query reversed)" if query_reversed else ""

        if not candidates:
            if delete_unmatched:
                print(f"[{i}/{total}] NO RESULTS — {red('deleting')}  {title_label}")
                if delete_song(db, row, dry_run):
                    deleted += 1
                else:
                    errors += 1
            else:
                print(f"[{i}/{total}] NO RESULTS  {title_label}")
                skipped += 1
            continue

        best = pick_best_auto(candidates, min_score)
        if best is None:
            top = candidates[0]
            if delete_unmatched:
                print(f"[{i}/{total}] LOW SCORE ({top['score']}){reversed_note} — {red('deleting')}  {title_label}"
                      f"  → best: {top['artist']} – {top['title']}")
                if delete_song(db, row, dry_run):
                    deleted += 1
                else:
                    errors += 1
            else:
                print(f"[{i}/{total}] LOW SCORE ({top['score']}){reversed_note}  {title_label}"
                      f"  → best: {top['artist']} – {top['title']}")
                skipped += 1
            continue

        print(f"[{i}/{total}] score={best['score']}{reversed_note}  "
              f"{best['artist']} – {best['title']}"
              f"  ({best.get('year', '')})")
        if apply_match(db, row, best, media_dir, dry_run):
            applied += 1
        else:
            errors += 1

    print()
    summary = f"Done. Applied: {applied}  Skipped: {skipped}  Errors: {errors}"
    if delete_unmatched:
        summary = f"Done. Applied: {applied}  Deleted: {deleted}  Skipped: {skipped}  Errors: {errors}"
    print(summary)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--no-artist", action="store_true",
                        help="Only process songs that have no artist set (default: all songs)")
    parser.add_argument("--youtube-only", action="store_true",
                        help="Only process songs whose filename ends with a YouTube video ID "
                             "(11 base64url chars, e.g. '_dQw4w9WgXcQ' or ' [dQw4w9WgXcQ]')")
    parser.add_argument("--auto", action="store_true",
                        help="Automatic mode: apply best result without prompting")
    parser.add_argument("--delete-unmatched", action="store_true",
                        help="Auto mode: delete file(s) and DB entry when no match meets the score threshold")
    parser.add_argument("--min-score", type=int, default=100, metavar="N",
                        help="Minimum MusicBrainz score to accept in auto mode (default: 100)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without renaming files or updating the DB")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="Stop after processing N songs")
    parser.add_argument("--offset", type=int, default=0, metavar="N",
                        help="Skip the first N songs (useful for resuming)")
    parser.add_argument("--db", type=Path, default=_DEFAULT_DB,
                        help=f"Path to superkaraoke.db (default: {_DEFAULT_DB})")
    parser.add_argument("--media-dir", type=Path, default=_DEFAULT_MEDIA,
                        help=f"Media root (default: {_DEFAULT_MEDIA})")
    args = parser.parse_args()

    if not args.db.exists():
        print(f"Error: database not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    db = sqlite3.connect(str(args.db))
    db.row_factory = sqlite3.Row

    songs = get_songs(db, args.offset, args.limit, no_artist_only=args.no_artist)

    if args.youtube_only:
        songs = [s for s in songs if _YOUTUBE_ID.search(Path(s["file_path"]).stem)]

    total = len(songs)
    if not total:
        scope = "songs without artist" if args.no_artist else "songs"
        qualifier = " with YouTube ID suffix" if args.youtube_only else ""
        print(f"No {scope}{qualifier} found.")
        db.close()
        return

    scope = "songs without artist" if args.no_artist else "songs"
    mode = "automatic" if args.auto else "interactive"
    print(f"{total} {scope}"
          + (" (YouTube ID suffix only)" if args.youtube_only else "")
          + f"  |  mode: {mode}"
          + (f"  |  min-score: {args.min_score}" if args.auto else "")
          + ("  |  delete-unmatched" if args.auto and args.delete_unmatched else "")
          + ("  |  DRY RUN" if args.dry_run else ""))

    if args.auto:
        auto_loop(songs, db, args.media_dir, args.dry_run, args.min_score,
                  delete_unmatched=args.delete_unmatched)
    else:
        if args.delete_unmatched:
            print("Warning: --delete-unmatched has no effect in interactive mode (use [d] prompt instead)",
                  file=sys.stderr)
        interactive_loop(songs, db, args.media_dir, args.dry_run)

    db.close()


if __name__ == "__main__":
    main()
