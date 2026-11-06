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
# YouTube video ID anywhere in the title, in [], (), or space-delimited
_ANY_YTID = re.compile(r'[\s_\-]*[\[\(]?[A-Za-z0-9_-]{11}[\]\)]?\s*$')
# Core noise words (karaoke/quality/uploader branding)
_NOISE_WORDS = (
    r'official(?:\s+(?:video|audio|music\s+video|lyric\s+video))?|'
    r'lyric(?:s)?(?:\s+video)?|music\s+video|audio|hd|4k|1080p|720p|'
    r'karaoke(?:\s+version)?|karaoke\s+song|instrumental|backing\s+track|'
    r'no\s+vocal|vocal\s+guide|with\s+lyrics|on\s+screen\s+lyrics|'
    r'sing[\s\-]?along|piano\s+karaoke|acoustic\s+karaoke|'
    r'karafun|singking|sing\s+king|stingray|zoom\s+karaoke|'
    r'tgsstudio|party\s+tyme|sunfly'
)
# Noise phrases YouTube/karaoke uploaders stack onto titles (bracketed)
_TITLE_NOISE = re.compile(
    rf'\s*[\(\[\{{][^\)\]\}}]*?(?:{_NOISE_WORDS})[^\)\]\}}]*[\)\]\}}]\s*',
    re.IGNORECASE,
)
# Trailing standalone "— Karaoke" / "- Official Video" without brackets
_TRAIL_DASH_NOISE = re.compile(
    rf'\s*[-–—]\s*(?:{_NOISE_WORDS})\s*$',
    re.IGNORECASE,
)
# Pipe-delimited noise segments (e.g. "Artist | Song | Karaoke Version | KaraFun")
# Drops any " | <noise>" segment regardless of position.
_PIPE_NOISE = re.compile(
    rf'\s*\|\s*[^|]*?(?:{_NOISE_WORDS})[^|]*?(?=\s*\||\s*$)',
    re.IGNORECASE,
)


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
# Match "in the style of Artist" anywhere (bracketed or bare), capturing the artist
_STYLE_OF_ANY = re.compile(
    r'in\s+the\s+style\s+of\s+([^\)\]\}\-–—\n]+?)(?=\s*[\)\]\}]|\s*[-–—]|\s*$)',
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


def extract_style_of_hint(title: str) -> str:
    """Pull just the 'in the style of …' artist, ignoring surrounding noise."""
    m = _STYLE_OF_ANY.search(title)
    return m.group(1).strip() if m else ''


def clean_youtube_title(title: str) -> str:
    """
    Normalize a YouTube video title for human display and for MB queries.
    Strips video-ID suffixes and common uploader noise
    (official/lyric/karaoke/hd/4k/… in parens, after a dash, or between
    pipe-delimited segments).
    """
    s = title.strip()
    # Repeatedly strip noise — titles often stack several phrases
    prev = None
    while prev != s:
        prev = s
        s = _TITLE_NOISE.sub(' ', s).strip()
        s = _TRAIL_DASH_NOISE.sub('', s).strip()
        s = _PIPE_NOISE.sub('', s).strip()
    # Trailing YouTube ID (either bracketed or raw after _/space/dash)
    s = _TRAIL_YTID.sub('', s).strip()
    s = _ANY_YTID.sub('', s).strip()
    # Collapse whitespace and leftover delimiters
    s = re.sub(r'\s{2,}', ' ', s).strip(' -–—_|')
    s = re.sub(r'\s*\|\s*\|+\s*', ' | ', s).strip(' |')
    return s


def split_title_candidates(title: str, fallback_artist: str = "") -> list[tuple[str, str]]:
    """
    Produce candidate (recording_title, recording_artist) pairs from a cleaned
    title. Uploaders are inconsistent about which side is which, so we try
    both orderings and let MusicBrainz confirm.

    Returns a list in query-priority order; duplicates removed.
    """
    s = title.strip()
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []

    def push(t: str, a: str):
        t = t.strip(' -–—_|').strip()
        a = a.strip(' -–—_|').strip()
        if not t:
            return
        key = (t.lower(), a.lower())
        if key in seen:
            return
        seen.add(key)
        out.append((t, a))

    for sep in (' - ', ' | ', ' – ', ' — '):
        if sep in s:
            parts = [p.strip() for p in s.split(sep) if p.strip()]
            if len(parts) >= 2:
                # First two parts, either ordering
                push(parts[0], parts[1])
                push(parts[1], parts[0])
                # Three+ parts (e.g. Song | Artist | Album): try the first
                # with each subsequent part as the artist candidate.
                for i in range(2, min(len(parts), 4)):
                    push(parts[0], parts[i])
                    push(parts[i], parts[0])
                    push(parts[1], parts[i])
                    push(parts[i], parts[1])

    # Finally, the untouched title with whatever artist we have
    push(s, fallback_artist)
    return out


def clean_for_mb_query(title: str) -> tuple[str, str]:
    """
    Prepare a raw title string for a MusicBrainz query.
    Strips YouTube ID suffixes and uploader noise, then extracts any
    'in the style of Artist'.  Returns (clean_title, artist_hint).
    """
    # Grab the "in the style of X" hint BEFORE noise-stripping — the noise
    # regex is eager and will swallow the whole "(Karaoke in the style of X)"
    # group along with the artist.
    artist_hint = extract_style_of_hint(title)
    cleaned = clean_youtube_title(title)
    clean_title, parsed_artist = strip_style_of(cleaned)
    return clean_title, artist_hint or parsed_artist


def pick_best_mb_match(candidates: list[dict], min_score: int = 100,
                       prefer_oldest: bool = True) -> Optional[dict]:
    """
    Return the best candidate at or above *min_score*, or None.

    Entries are first gathered within the top-score *band* (top score and
    anything within 5 points of it — MusicBrainz treats these as equally
    good matches). Among the band:
      - prefer_oldest=True  → earliest year wins (original pressing).
      - prefer_oldest=False → top score wins outright, year tie-breaks.
    Entries without a year sort last in either mode.
    """
    eligible = [c for c in candidates if c.get("score", 0) >= min_score]
    if not eligible:
        return None
    top_score = max(c["score"] for c in eligible)
    band = [c for c in eligible if c["score"] >= top_score - 5]
    if prefer_oldest:
        band.sort(key=lambda c: (c.get("year") is None, c.get("year") or 0))
    else:
        band = [c for c in band if c["score"] == top_score]
        band.sort(key=lambda c: (c.get("year") is None, c.get("year") or 0))
    return band[0]


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
