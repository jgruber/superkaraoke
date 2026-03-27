"""
Metadata extraction for karaoke files.

Priority order for title/artist:
  1. Embedded file tags (mutagen: ID3 for MP3, atoms for MP4, etc.)
  2. Filename parsing (common karaoke patterns: "Artist - Title")

Online lookup via MusicBrainz (free, no API key, 1 req/sec limit).
"""
import re
import logging
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ── Filename parsing ───────────────────────────────────────────────────────────

# Leading publisher/catalog tags: [SC], (KJ), SC-, KJ_
_LEAD_TAG   = re.compile(r'^(?:[\[\(][^\]\)]{1,10}[\]\)]\s*|[A-Z]{2,4}[-_]\s*)')
# Leading track numbers: "01 ", "01. ", "01 - "
_TRACK_NUM  = re.compile(r'^\d{1,3}[\s.\-]+')
# Trailing karaoke/quality markers
_TRAIL_TAG  = re.compile(
    r'\s*[\(\[](karaoke|instrumental|backing\s+track|no\s+vocal|vocal\s+guide|'
    r'hd|4k|1080p|720p)[^\)\]]*[\)\]]\s*$',
    re.IGNORECASE,
)
# Trailing " - Karaoke Version" style suffixes
_TRAIL_SUFFIX = re.compile(
    r'\s*[-–]\s*(karaoke|instrumental|karaoke\s+version)\s*$',
    re.IGNORECASE,
)
# YouTube video ID appended to filename: "Title_dQw4w9WgXcQ" or "Title [dQw4w9WgXcQ]"
_TRAIL_YTID = re.compile(r'[\s_]\[?[A-Za-z0-9_-]{11}\]?\s*$')


def parse_filename(stem: str) -> dict:
    """
    Return {'title': str, 'artist': str} extracted from a filename stem.
    Most common karaoke convention: "Artist - Title".
    """
    s = stem.strip()
    s = _LEAD_TAG.sub('', s).strip()
    s = _TRACK_NUM.sub('', s).strip()
    s = _TRAIL_TAG.sub('', s).strip()
    s = _TRAIL_SUFFIX.sub('', s).strip()
    s = _TRAIL_YTID.sub('', s).strip()

    if ' - ' in s:
        artist, title = s.split(' - ', 1)
        return {'artist': artist.strip(), 'title': title.strip()}

    return {'artist': '', 'title': s or stem}


# ── Mutagen tag reading ────────────────────────────────────────────────────────

def _first(val) -> str:
    """Return first element if list/tuple, else str."""
    if val is None:
        return ''
    if isinstance(val, (list, tuple)):
        return str(val[0]).strip() if val else ''
    return str(val).strip()


def read_file_tags(file_path: str) -> dict:
    """
    Read embedded metadata from audio/video file via mutagen.
    Returns a (possibly empty) dict with keys: title, artist, year, genre, duration_secs.
    """
    try:
        from mutagen import File as MutagenFile  # noqa: PLC0415
        mf = MutagenFile(file_path, easy=True)
        if mf is None:
            return {}

        result: dict = {}

        # Duration is always available via info, regardless of tags
        if hasattr(mf, 'info') and mf.info and hasattr(mf.info, 'length'):
            length = mf.info.length
            if length and length > 0:
                result['duration_secs'] = round(length, 2)

        if mf.tags is None:
            return result

        tags = mf.tags

        if title := _first(tags.get('title')):
            result['title'] = title
        if artist := _first(tags.get('artist') or tags.get('albumartist')):
            result['artist'] = artist
        if genre := _first(tags.get('genre')):
            result['genre'] = genre
        if date := _first(tags.get('date')):
            m = re.match(r'(\d{4})', date)
            if m:
                result['year'] = int(m.group(1))

        return result
    except Exception as exc:
        log.debug("mutagen read failed for %s: %s", file_path, exc)
        return {}


def extract_metadata(file_path: str, cdg_path: Optional[str], kind: str,
                     read_tags: bool = True) -> dict:
    """
    Best-effort metadata for a song file.

    For CDG+MP3 pairs (kind='cdg'), file tags are skipped by default:
    the 'Artist - Title' filename convention is reliable and avoids slow
    network reads on NAS-mounted libraries.  Pass read_tags=True to force
    a mutagen read (e.g. from the library 're-detect' action).

    For video files, mutagen is always attempted since filenames are less
    structured and MP4/MKV containers often carry good embedded metadata.
    """
    parsed = parse_filename(Path(file_path).stem)

    # Always read via mutagen — duration comes from audio info (fast header read).
    # For CDG, tag fields (title/artist) are ignored in favour of filename parsing.
    tags = read_file_tags(file_path)

    return {
        'title':        tags.get('title')  or parsed.get('title')  or Path(file_path).stem,
        'artist':       tags.get('artist') or parsed.get('artist') or '',
        'year':         tags.get('year'),
        'genre':        tags.get('genre')  or '',
        'duration_secs': tags.get('duration_secs'),
    }


# ── MusicBrainz query helpers ─────────────────────────────────────────────────

_STYLE_OF = re.compile(
    r'^(.+?)\s+\(?\s*in\s+the\s+style\s+of\s+(.+?)\s*\)?\s*$',
    re.IGNORECASE,
)


def strip_style_of(title: str) -> tuple[str, str]:
    """
    If *title* contains the karaoke convention '… in the style of Artist',
    return (clean_title, artist).  Otherwise return (title, '').
    """
    m = _STYLE_OF.match(title.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return title, ''


def clean_for_mb_query(title: str) -> tuple[str, str]:
    """
    Prepare a raw title string for a MusicBrainz query.
    Strips YouTube ID suffixes, then extracts any 'in the style of Artist'.
    Returns (clean_title, artist_hint).
    """
    title = _TRAIL_YTID.sub('', title).strip()
    return strip_style_of(title)


def pick_best_mb_match(candidates: list[dict], min_score: int = 100) -> Optional[dict]:
    """
    Return the best candidate at or above *min_score*, or None.
    Only candidates at the top score are considered.
    Among ties, prefer the earliest release year (no-year entries sort last).
    """
    eligible = [c for c in candidates if c.get("score", 0) >= min_score]
    if not eligible:
        return None
    top_score = max(c["score"] for c in eligible)
    top = [c for c in eligible if c["score"] == top_score]
    top.sort(key=lambda c: (c.get("year") is None, c.get("year") or 0))
    return top[0]


# ── MusicBrainz lookup ─────────────────────────────────────────────────────────

_MB_API     = "https://musicbrainz.org/ws/2/recording/"
_MB_HEADERS = {"User-Agent": "SuperKaraoke/1.0 (https://github.com/superkaraoke)"}
_MB_LIMIT   = 1.1   # seconds between calls (MusicBrainz rate limit: 1 req/sec)
_last_call: float = 0.0


async def search_musicbrainz(title: str, artist: str = "") -> list[dict]:
    """
    Search MusicBrainz recordings.  Returns up to 10 candidates:
      [{mb_id, title, artist, year, genre, score}, ...]
    Rate-limited to avoid 503s.
    """
    import asyncio
    import httpx

    global _last_call
    gap = _MB_LIMIT - (time.monotonic() - _last_call)
    if gap > 0:
        await asyncio.sleep(gap)
    _last_call = time.monotonic()

    parts = []
    if title:
        parts.append(f'recording:"{title}"')
    if artist:
        parts.append(f'artist:"{artist}"')
    query = ' AND '.join(parts) if parts else title

    try:
        async with httpx.AsyncClient(headers=_MB_HEADERS, timeout=12.0) as client:
            resp = await client.get(
                _MB_API,
                params={"query": query, "fmt": "json", "limit": "10"},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        log.warning("MusicBrainz lookup failed: %s", exc)
        return []

    results = []
    for rec in data.get("recordings", []):
        credits = rec.get("artist-credit", [])
        mb_artist = " & ".join(
            c.get("name") or c.get("artist", {}).get("name", "")
            for c in credits
            if isinstance(c, dict)
        ).strip()

        date_str = rec.get("first-release-date", "")
        year = int(date_str[:4]) if len(date_str) >= 4 and date_str[:4].isdigit() else None

        raw_tags = sorted(rec.get("tags", []), key=lambda t: -t.get("count", 0))
        genre = raw_tags[0]["name"].title() if raw_tags else ""

        results.append({
            "mb_id":  rec.get("id", ""),
            "title":  rec.get("title", ""),
            "artist": mb_artist,
            "year":   year,
            "genre":  genre,
            "score":  rec.get("score", 0),
        })

    return results
