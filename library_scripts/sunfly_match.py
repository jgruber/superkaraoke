#!/usr/bin/env python3
"""
sunfly_match.py — Match Sunfly CDG/MP3 files that have no artist set to the
Sunfly karaoke catalogue PDF, rename their files, and update the database.

Matching strategy
─────────────────
1. The SF album number is extracted from the directory path  (…/SF NNN/… → NNN).
2. A track number is extracted from the filename via several known patterns:
     • sfNNN-TT_…          (e.g. sf004-11_stewart,_rod_…)
     • SFNNN-TT …          (e.g. SF100-15 GREEN TAMBOURINE)
     • Artist_-_Title_-_SFNNN-TT   (e.g. Bananarama_-_Nathan_Jones_-_SF151-10)
     • SF TT TITLE         (e.g. SF 01 WHAT A BEAUTIFUL DAY)
     • TT-Title-Artist     (e.g. 01-I_Love_You_Because-Jim_Reeves)
     • TT TITLE            (e.g. 09 CONSTANT CRAVING)
3. The (album, track) pair is looked up in the PDF catalogue → exact match.
4. If no track can be extracted (or no exact match), a fuzzy title match is
   attempted against all songs in that album.

Rename convention
─────────────────
Files are renamed to "Artist - Title.ext" so the library scanner parses them
correctly on the next rescan. For CDG+MP3 pairs, both files are renamed.

The database title, artist, file_path, cdg_path, and id columns are updated
in-place; the metadata_locked flag is set so future rescans do not overwrite
the corrections.

Usage
─────
  # Preview what would change (no files or DB touched)
  python3 sunfly_match.py --dry-run

  # Apply changes
  python3 sunfly_match.py

  # Override paths
  python3 sunfly_match.py --pdf "Sunfly karaoke list.pdf" \\
      --db superkaraoke.db --media-dir /media/karaoke

  # Only process specific albums (by SF number)
  python3 sunfly_match.py --albums 52 100 114 --dry-run

  # Lower the fuzzy-match confidence threshold (0–100, default 70)
  python3 sunfly_match.py --fuzzy-threshold 60 --dry-run
"""

import argparse
import difflib
import hashlib
import json
import logging
import re
import sqlite3
import sys
from pathlib import Path

# ── Defaults ──────────────────────────────────────────────────────────────────

_DEFAULT_PDF   = Path("/data/Sunfly karaoke list.pdf")
_DEFAULT_DB    = Path("/data/superkaraoke.db")
_DEFAULT_MEDIA = Path("/media/karaoke")

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ── PDF parsing ───────────────────────────────────────────────────────────────

def _load_catalogue(rows: list) -> tuple[dict, dict]:
    """Build exact and by_album dicts from a list of [album_int, track_int, song, artist] rows."""
    exact: dict[tuple[int, int], dict] = {}
    by_album: dict[int, list] = {}
    for album_int, track_int, song, artist in rows:
        entry = {"track": track_int, "song": song, "artist": artist}
        exact[(album_int, track_int)] = {"song": song, "artist": artist}
        by_album.setdefault(album_int, []).append(entry)
    return exact, by_album


def parse_pdf(pdf_path: Path, cache_path: Path | None = None) -> tuple[dict, dict]:
    """
    Parse the Sunfly catalogue PDF.

    If cache_path is given and exists, load from there instead of re-parsing
    the PDF (much faster). Pass --rebuild-cache to force a fresh parse.

    Returns
    -------
    exact : dict  {(album_int, track_int): {'song': str, 'artist': str}}
    by_album : dict  {album_int: list of {'track': int, 'song': str, 'artist': str}}
    """
    # Try cache first
    if cache_path and cache_path.exists():
        log.info("Loading catalogue from cache: %s", cache_path)
        rows = json.loads(cache_path.read_text())
        exact, by_album = _load_catalogue(
            [(r[0], r[1], r[2], r[3]) for r in rows]
        )
        log.info("PDF cache: %d entries across %d albums", len(exact), len(by_album))
        return exact, by_album

    try:
        import pdfplumber
    except ImportError:
        log.error("pdfplumber is required: pip install pdfplumber")
        sys.exit(1)

    rows = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        log.info("Parsing %d PDF pages (this takes ~3 min; result will be cached)…",
                 len(pdf.pages))
        for page in pdf.pages:
            table = page.extract_table()
            if not table:
                continue
            for row in table:
                if not row or len(row) < 5:
                    continue
                # Skip header rows
                if not row[1] or not row[1].startswith("SF"):
                    continue
                try:
                    album_code = row[1].strip()          # "SF283"
                    album_int  = int(album_code[2:])     # 283
                    track_int  = int(row[2].strip())
                    song       = (row[3] or "").strip()
                    artist     = (row[4] or "").strip()
                except (ValueError, AttributeError, TypeError):
                    continue
                rows.append([album_int, track_int, song, artist])

    exact, by_album = _load_catalogue(rows)
    log.info("PDF: %d unique (album, track) entries across %d albums",
             len(exact), len(by_album))

    if cache_path:
        cache_path.write_text(json.dumps(rows))
        log.info("Catalogue cached to %s", cache_path)

    return exact, by_album


# ── Text normalisation for fuzzy matching ─────────────────────────────────────

_NON_ALNUM = re.compile(r'[^a-z0-9\s]')
_MULTI_SP  = re.compile(r'\s+')

def _norm(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    s = s.lower()
    s = s.replace('_', ' ')
    s = _NON_ALNUM.sub('', s)
    return _MULTI_SP.sub(' ', s).strip()


def fuzzy_score(a: str, b: str) -> int:
    """0–100 similarity score using SequenceMatcher."""
    return round(
        difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio() * 100
    )


# ── Album / track extraction from file paths ──────────────────────────────────

# Directory: …/SF NNN/… or …/SF NNN (end)
_DIR_ALBUM = re.compile(r'/SF\s+(\d+)/?', re.IGNORECASE)

# Filename patterns (applied in order to the stem, i.e. no extension):
#   1a. sfNNN[-space]TT (with optional whitespace around dash; permissive end)
#       handles: sf004-11_  SF181-01-Title  SF183-10Title  SF215 -09 Title
_PAT_SF_TRACK  = re.compile(r'^sf\s*(\d{2,})\s*-\s*(\d{1,2})(?!\d)', re.IGNORECASE)
#   1b. sfNNNTT (album+track merged without dash, e.g. sf23501 title)
_PAT_SF_MERGE  = re.compile(r'^sf(\d{3})(\d{2})[\s\W]', re.IGNORECASE)
#   1c. SF_NNN_SONG_TT_TITLE  (e.g. "SF_139_SONG_09_BAGGY_TROUSE")
_PAT_SF_SONG   = re.compile(r'^SF_(\d{2,})_SONG_(\d{1,2})_', re.IGNORECASE)
#   2. Artist_-_Title_-_SFNNN-TT  at end
_PAT_SF_END    = re.compile(r'[_-]sf(\d{2,})-(\d{1,2})$', re.IGNORECASE)
#   3. SF TT TITLE  (e.g. "SF 01 WHAT A BEAUTIFUL DAY")
_PAT_SF_SPACE  = re.compile(r'^SF\s+(\d{1,2})\s+', re.IGNORECASE)
#   4. TT prefix or bare TT (e.g. "01-", "09 ", "01___", or just "11")
_PAT_TRACK     = re.compile(r'^(\d{1,2})([\s._\-]+|$)')


def extract_album_from_path(file_path: str) -> int | None:
    """Return SF album number from the directory portion of file_path, or None."""
    m = _DIR_ALBUM.search(file_path)
    if m:
        return int(m.group(1))
    return None


def extract_track_from_stem(stem: str) -> tuple[int | None, int | None]:
    """
    Return (album_from_filename, track_num) from a filename stem.
    album_from_filename is only set when the album number is embedded in
    the filename itself (patterns 1 and 2); otherwise it is None.
    track_num is None if no track number could be found.
    """
    # Pattern 1a: sfNNN-TT (permissive: optional whitespace, no requirement after track)
    m = _PAT_SF_TRACK.match(stem)
    if m:
        return int(m.group(1)), int(m.group(2))

    # Pattern 1b: sfNNNTT merged (e.g. sf23501 title)
    m = _PAT_SF_MERGE.match(stem)
    if m:
        return int(m.group(1)), int(m.group(2))

    # Pattern 1c: SF_NNN_SONG_TT_Title
    m = _PAT_SF_SONG.match(stem)
    if m:
        return int(m.group(1)), int(m.group(2))

    # Pattern 2: …-SFNNN-TT at end
    m = _PAT_SF_END.search(stem)
    if m:
        return int(m.group(1)), int(m.group(2))

    # Pattern 3: SF TT TITLE
    m = _PAT_SF_SPACE.match(stem)
    if m:
        return None, int(m.group(1))

    # Pattern 4: plain track-number prefix  (01- / 01  / 02- / 09 )
    m = _PAT_TRACK.match(stem)
    if m:
        track = int(m.group(1))
        if 1 <= track <= 20:          # sanity: real tracks are 1–20
            return None, track

    return None, None


# ── Title-casing helper ───────────────────────────────────────────────────────

# Words that stay lowercase unless they're first/last
_LOWER_WORDS = {
    "a", "an", "the", "and", "but", "or", "nor", "for", "so", "yet",
    "at", "by", "in", "of", "on", "to", "up", "as", "if", "is",
}

def title_case(s: str) -> str:
    """Title-case a string, keeping small words lowercase in the middle."""
    words = s.split()
    result = []
    for i, w in enumerate(words):
        if i == 0 or i == len(words) - 1 or w.lower() not in _LOWER_WORDS:
            result.append(w.capitalize())
        else:
            result.append(w.lower())
    return " ".join(result)


def safe_filename(s: str) -> str:
    """Remove characters that are unsafe in filenames."""
    return re.sub(r'[<>:"/\\|?*]', '', s).strip()


# ── Song ID (must match server/database.py) ───────────────────────────────────

def song_id(path: Path, media_dir: Path) -> str:
    rel = str(path.relative_to(media_dir))
    return hashlib.sha256(rel.encode()).hexdigest()[:12]


# ── Database helpers ──────────────────────────────────────────────────────────

def get_sunfly_songs(db: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return songs with empty artist that are in a Sun Fly/SF directory."""
    return db.execute("""
        SELECT id, file_path, cdg_path, title, artist, kind, metadata_locked
        FROM songs
        WHERE (artist = '' OR artist IS NULL)
          AND file_path LIKE '%/SF %/%'
        ORDER BY file_path
    """).fetchall()


def update_song_db(db: sqlite3.Connection,
                   old_id: str,
                   new_id: str,
                   new_file_path: str,
                   new_cdg_path: str | None,
                   new_title: str,
                   new_artist: str) -> None:
    """Update the songs row; guard against duplicate new IDs."""
    existing = db.execute(
        "SELECT id FROM songs WHERE id = ? AND id != ?", (new_id, old_id)
    ).fetchone()
    if existing:
        log.warning("  new id %s already in DB — removing old row only", new_id)
        db.execute("DELETE FROM songs WHERE id = ?", (old_id,))
    else:
        db.execute("""
            UPDATE songs
               SET id              = ?,
                   file_path       = ?,
                   cdg_path        = ?,
                   title           = ?,
                   artist          = ?,
                   metadata_locked = 1
             WHERE id = ?
        """, (new_id, new_file_path, new_cdg_path, new_title, new_artist, old_id))
    db.commit()


# ── Main matching loop ────────────────────────────────────────────────────────

def match_and_apply(
    songs: list[sqlite3.Row],
    exact: dict,
    by_album: dict,
    db: sqlite3.Connection,
    media_dir: Path,
    fuzzy_threshold: int,
    album_filter: set[int] | None,
    dry_run: bool,
) -> None:

    stats = dict(exact_match=0, fuzzy_match=0, no_match=0, skipped=0,
                 rename_ok=0, rename_fail=0, db_updated=0)

    for row in songs:
        old_id    = row["id"]
        fp        = row["file_path"]
        cp        = row["cdg_path"]
        kind      = row["kind"]
        db_title  = row["title"] or ""

        path = Path(fp)
        stem = path.stem
        ext  = path.suffix.lower()       # ".mp3" or ".mp4"

        # ── Extract album from directory ───────────────────────────────────
        album = extract_album_from_path(fp)
        if album is None:
            log.debug("No album in path: %s", fp)
            stats["skipped"] += 1
            continue

        if album_filter and album not in album_filter:
            continue

        if album not in by_album:
            log.debug("[SF%03d] not in PDF — skipping %s", album, path.name)
            stats["skipped"] += 1
            continue

        # ── Extract track from filename ────────────────────────────────────
        fn_album, track = extract_track_from_stem(stem)

        # Cross-check: if filename embeds an album number, it must match dir
        if fn_album is not None and fn_album != album:
            log.warning("[SF%03d] filename says SF%03d — skipping %s",
                        album, fn_album, path.name)
            stats["skipped"] += 1
            continue

        # ── Lookup ────────────────────────────────────────────────────────
        match_how = ""
        match_score = 100
        catalogue_entry = None

        if track is not None:
            catalogue_entry = exact.get((album, track))
            if catalogue_entry:
                match_how = f"exact (SF{album:03d} track {track})"

        # Fuzzy fallback: compare db_title / stem against all album entries
        if catalogue_entry is None:
            best_score = 0
            best_entry = None
            query = db_title or stem
            for entry in by_album[album]:
                # Try matching against song title only, then against "artist song"
                # combined — catches filenames like "Minnie_Ripperton_Loving_You"
                # where the db title includes the artist prefix.
                s1 = fuzzy_score(query, entry["song"])
                s2 = fuzzy_score(query, f"{entry['artist']} {entry['song']}")
                score = max(s1, s2)
                if score > best_score:
                    best_score = score
                    best_entry = entry
            if best_entry and best_score >= fuzzy_threshold:
                catalogue_entry = best_entry
                match_score = best_score
                match_how = (f"fuzzy '{_norm(query)}' ≈ '{_norm(best_entry['song'])}' "
                             f"({best_score}%)")
            else:
                log.info("[SF%03d] NO MATCH  %s", album, path.name)
                if best_entry:
                    log.info("         best fuzzy: '%s' by '%s' (%d%%)",
                             best_entry["song"], best_entry["artist"], best_score)
                stats["no_match"] += 1
                continue

        if "exact" in match_how:
            stats["exact_match"] += 1
        else:
            stats["fuzzy_match"] += 1

        # ── Build new names ────────────────────────────────────────────────
        pdf_song   = catalogue_entry["song"]
        pdf_artist = catalogue_entry["artist"]

        new_title  = title_case(pdf_song)
        new_artist = title_case(pdf_artist)

        new_stem   = safe_filename(f"{new_artist} - {new_title}")
        new_mp3    = path.parent / f"{new_stem}{ext}"
        new_cdg    = (Path(cp).parent / f"{new_stem}.cdg") if cp else None

        new_file_path = str(new_mp3)
        new_cdg_path  = str(new_cdg) if new_cdg else None

        try:
            new_id = song_id(new_mp3, media_dir)
        except ValueError:
            log.warning("  Cannot compute new ID for %s — skipping", new_mp3)
            stats["skipped"] += 1
            continue

        # ── Dry-run output ─────────────────────────────────────────────────
        if dry_run:
            print()
            print(f"  FILE    : {path.name}")
            print(f"  MATCH   : {match_how}")
            print(f"  PDF     : '{pdf_song}' by '{pdf_artist}'")
            print(f"  RENAME  → {new_mp3.name}")
            if new_cdg:
                print(f"  CDG     → {new_cdg.name}")
            print(f"  DB      : title='{new_title}', artist='{new_artist}', id {old_id}→{new_id}")
            continue

        # ── Rename files ───────────────────────────────────────────────────
        renamed_mp3 = False
        renamed_cdg = False

        if path == new_mp3:
            renamed_mp3 = True        # already has the right name
        elif not path.exists():
            log.warning("  Source not found: %s", fp)
            stats["skipped"] += 1
            continue
        else:
            try:
                path.rename(new_mp3)
                renamed_mp3 = True
                log.info("  RENAMED %s → %s", path.name, new_mp3.name)
            except OSError as e:
                log.error("  RENAME FAILED %s: %s", path.name, e)
                stats["rename_fail"] += 1
                continue

        if cp and new_cdg:
            cdg_path = Path(cp)
            if cdg_path == new_cdg:
                renamed_cdg = True
            elif cdg_path.exists():
                try:
                    cdg_path.rename(new_cdg)
                    renamed_cdg = True
                    log.info("  RENAMED %s → %s", cdg_path.name, new_cdg.name)
                except OSError as e:
                    log.warning("  CDG rename failed: %s", e)
            else:
                log.warning("  CDG not found: %s", cp)

        stats["rename_ok"] += 1

        # ── Update database ────────────────────────────────────────────────
        update_song_db(
            db        = db,
            old_id    = old_id,
            new_id    = new_id,
            new_file_path = new_file_path,
            new_cdg_path  = new_cdg_path if (cp and renamed_cdg) else (new_cdg_path if cp else None),
            new_title  = new_title,
            new_artist = new_artist,
        )
        stats["db_updated"] += 1
        log.info("  ✓ [SF%03d] '%s' by '%s' (%s)", album, new_title, new_artist, match_how)

    # ── Summary ───────────────────────────────────────────────────────────
    print()
    if dry_run:
        print(f"DRY RUN — no changes made.")
        print(f"  Exact matches : {stats['exact_match']}")
        print(f"  Fuzzy matches : {stats['fuzzy_match']}")
        print(f"  No match      : {stats['no_match']}")
        print(f"  Skipped       : {stats['skipped']}")
    else:
        print(f"Done.")
        print(f"  Exact matches : {stats['exact_match']}")
        print(f"  Fuzzy matches : {stats['fuzzy_match']}")
        print(f"  No match      : {stats['no_match']}")
        print(f"  Skipped       : {stats['skipped']}")
        print(f"  Files renamed : {stats['rename_ok']}")
        print(f"  Rename failed : {stats['rename_fail']}")
        print(f"  DB rows updated: {stats['db_updated']}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--pdf", type=Path, default=_DEFAULT_PDF,
                        help=f"Path to the Sunfly karaoke PDF (default: {_DEFAULT_PDF.name})")
    parser.add_argument("--rebuild-cache", action="store_true",
                        help="Re-parse the PDF even if a cache file exists")
    parser.add_argument("--db", type=Path, default=_DEFAULT_DB,
                        help=f"Path to superkaraoke.db (default: {_DEFAULT_DB})")
    parser.add_argument("--media-dir", type=Path, default=_DEFAULT_MEDIA,
                        help=f"Media root directory (default: {_DEFAULT_MEDIA})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show matches without renaming files or updating the database")
    parser.add_argument("--albums", nargs="+", type=int, metavar="N",
                        help="Only process these SF album numbers (e.g. --albums 52 100 114)")
    parser.add_argument("--fuzzy-threshold", type=int, default=70, metavar="N",
                        help="Minimum fuzzy-match score 0–100 to accept (default: 70)")
    args = parser.parse_args()

    if not args.pdf.exists():
        log.error("PDF not found: %s", args.pdf)
        sys.exit(1)
    if not args.db.exists():
        log.error("Database not found: %s", args.db)
        sys.exit(1)

    album_filter = set(args.albums) if args.albums else None

    cache_path = args.pdf.with_suffix(".catalogue.json")
    if args.rebuild_cache and cache_path.exists():
        cache_path.unlink()

    log.info("Parsing PDF: %s", args.pdf)
    exact, by_album = parse_pdf(args.pdf, cache_path=cache_path)

    db = sqlite3.connect(str(args.db))
    db.row_factory = sqlite3.Row

    songs = get_sunfly_songs(db)
    log.info("DB: %d songs with empty artist in SF directories", len(songs))

    if args.dry_run:
        log.info("DRY RUN — no files or database will be modified")

    match_and_apply(
        songs           = songs,
        exact           = exact,
        by_album        = by_album,
        db              = db,
        media_dir       = args.media_dir,
        fuzzy_threshold = args.fuzzy_threshold,
        album_filter    = album_filter,
        dry_run         = args.dry_run,
    )

    db.close()


if __name__ == "__main__":
    main()
