# SuperKaraoke

A modern, self-hosted karaoke server with multi-screen synchronized playback, per-screen pitch shifting, a searchable song library, and a mobile-friendly web interface.

Built as a spiritual successor to [pikaraoke](https://github.com/jgruber/pikaraoke), with a key architectural improvement: **all streaming runs through a single HTTP port**, making it fully reverse-proxy friendly.

---

## Features

- **Single-port design** — ffmpeg audio/video streams are piped through FastAPI's `StreamingResponse`. No second port, no firewall exceptions, works behind any reverse proxy.
- **CDG+MP3 and video support** — reads `.cdg`+`.mp3` karaoke pairs and standalone video files (`.mp4`, `.mkv`, `.avi`, `.webm`, `.mov`).
- **Multi-screen synchronized playback** — all `/screen` clients subscribe to the same broadcast stream (one ffmpeg process, fan-out queue per subscriber). Screens stay in sync without any clock negotiation.
- **Per-screen pitch shifting** — each display screen can independently shift the key up/down ±12 semitones using ffmpeg's rubberband filter. Screens at the default key share one stream; pitched screens get their own dedicated process.
- **Persistent song library** — songs are stored in SQLite with title, artist, year, genre, and like count. Metadata is auto-detected from embedded file tags (mutagen) and filename patterns (`Artist - Title`).
- **Library management UI** — edit any song's metadata, re-detect from file, or look up correct attributes via the MusicBrainz database.
- **Real-time queue** — Alpine.js UI updates instantly via WebSocket. Add songs, reorder, remove, or skip from any device.
- **Like system** — heart any song; sort the library by most-liked.
- **Dark / light mode** — Tailwind CSS class-based theming, persists to `localStorage`, auto-detects system preference.
- **Mobile-friendly** — responsive layout works on phones and tablets.

---

## Requirements

- Python 3.11+
- Node.js 18+ and npm (for the frontend build)
- ffmpeg 6+ with the **rubberband** filter compiled in (required for pitch shifting; standard Ubuntu/Debian packages include it)

Verify:

```bash
ffmpeg -filters 2>/dev/null | grep rubberband
python3 --version
node --version
```

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/jgruber/superkaraoke.git
cd superkaraoke
```

### 2. Create a Python virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Build the frontend

```bash
cd frontend
npm install
npm run build
cd ..
```

This produces `frontend/dist/` which the server serves automatically.

---

## Running

### Basic usage

```bash
SK_MEDIA_DIR=/path/to/your/karaoke .venv/bin/python run.py
```

Open `http://localhost:8080` in a browser.

### CLI options

```
python run.py [--host HOST] [--port PORT] [--media-dir PATH] [--reload]

  --host        Bind address (default: 0.0.0.0)
  --port        HTTP port (default: 8080)
  --media-dir   Path to karaoke media directory (default: /tmp/karaoke)
  --reload      Enable auto-reload for development
```

### Environment variables

All settings can be set via environment variables with the `SK_` prefix, or in a `.env` file in the project root:

| Variable | Default | Description |
|---|---|---|
| `SK_MEDIA_DIR` | `/tmp/karaoke` | Path to karaoke media files |
| `SK_PORT` | `8080` | HTTP server port |
| `SK_HOST` | `0.0.0.0` | Bind address |
| `SK_DB_PATH` | `superkaraoke.db` | SQLite database file path |
| `SK_FFMPEG_LOGLEVEL` | `warning` | ffmpeg log verbosity |
| `SK_ALLOWED_NETWORKS` | `` | Comma-separated CIDR subnets that bypass authentication (e.g. `192.168.1.0/24,10.0.0.0/8`) |

Example `.env`:

```env
SK_MEDIA_DIR=/media/karaoke
SK_PORT=8080
SK_DB_PATH=/var/lib/superkaraoke/library.db
```

---

## Authentication

SuperKaraoke supports optional per-user authentication for remote clients.

### How it works

| Client source | Access |
|---|---|
| IP in `SK_ALLOWED_NETWORKS` | Always allowed — no login required ("local" mode) |
| Any IP (no networks configured) | Must log in with username + password |
| No users created yet | **Bootstrap mode** — everyone is treated as local so you can reach the UI and create the first account |

Local users still get the name-prompt when queueing a song (saved in `localStorage`). Authenticated users always queue under their login name — the client-supplied name is ignored.

### Credentials file

Accounts are stored in `credentials.json` in the same directory as the database (`/data/credentials.json` in Docker). Passwords are hashed with PBKDF2-SHA256 (stdlib — no extra dependencies).

```json
{
  "users": {
    "alice": { "password_hash": "pbkdf2:sha256:260000:<salt>:<hash>" },
    "bob":   { "password_hash": "pbkdf2:sha256:260000:<salt>:<hash>" }
  }
}
```

### User management

Click the **profile chip** (top-right of the queue page) → **Manage users** to open the user management modal. From there you can create accounts, change passwords, and delete users. The modal is available to:

- Clients on an allowed network
- Authenticated remote users (who can only change their own password unless on a local network)

### Docker example with a local subnet

```bash
docker run -d \
  --name superkaraoke \
  -p 8080:8080 \
  -v /path/to/your/karaoke:/media/karaoke \
  -v superkaraoke_data:/data \
  -e SK_ALLOWED_NETWORKS="192.168.1.0/24" \
  --restart unless-stopped \
  superkaraoke
```

Clients on `192.168.1.x` (your LAN) get in without a password. Everyone else sees the login screen.

### Nginx note

When running behind nginx on the same host, the direct TCP connection appears as `127.0.0.1`. SuperKaraoke automatically trusts `X-Forwarded-For` in that case, so the real client IP is used for network matching. For other proxy setups, add the proxy's IP to `SK_ALLOWED_NETWORKS` or ensure it forwards the real IP.

---

## URLs

| Path | Description |
|---|---|
| `/` | User interface — search songs, manage queue, like songs |
| `/screen` | Display screen — fullscreen video player for a TV/projector |
| `/library` | Library management — edit metadata, MusicBrainz lookup, rescan |
| `/health` | Health check JSON |

Open `/screen` on each display device (TV, projector, spare monitor). Open `/` on phones and laptops to browse and queue songs.

---

## Media directory layout

SuperKaraoke recursively scans the configured media directory. Supported formats:

**CDG karaoke** — a `.mp3` and `.cdg` file with the same base name in the same directory:
```
karaoke/
  Pop/
    ABBA - Dancing Queen.mp3
    ABBA - Dancing Queen.cdg
  Rock/
    Eagles - Hotel California.mp3
    Eagles - Hotel California.cdg
```

**Video karaoke** — standalone video files:
```
karaoke/
  Videos/
    Queen - Bohemian Rhapsody.mp4
    David Bowie - Heroes.mkv
```

### Filename conventions

The scanner recognises the common `Artist - Title` pattern and strips:
- Leading publisher/catalog tags: `[SC]`, `(KJ)`, `SC-`
- Leading track numbers: `01 `, `01. `, `01 - `
- Trailing karaoke markers: `(Karaoke)`, `(Instrumental)`, `- Karaoke Version`

If a file also has embedded ID3 or MP4 tags, those take priority over the filename.

---

## Library management

Go to `/library` to:

- **Search and sort** the full library by title, artist, year, genre, or like count
- **Edit metadata** — click the Edit button on any row to open the edit modal
- **Re-detect from file** — re-reads embedded tags and filename; resets the metadata lock
- **MusicBrainz lookup** — searches the open MusicBrainz database for correct metadata; click a result to populate the form
- **Lock metadata** — check "Lock metadata" before saving to prevent future rescans from overwriting your corrections
- **Rescan library** — picks up new files or removed files without restarting the server

---

## Pitch shifting

Each `/screen` page has a **♭ / ♯** control in the bottom-left corner. Adjusting it reconnects that screen to a dedicated ffmpeg stream with the rubberband pitch filter applied. Other screens at the default key are unaffected and share the original stream.

Range: ±12 semitones (one octave each way). Reset with the ↺ button.

> **Note:** Changing pitch mid-song restarts playback from the beginning on that screen.

---

## Reverse proxy (nginx)

```nginx
server {
    listen 80;
    server_name karaoke.local;

    location / {
        proxy_pass         http://127.0.0.1:8080;
        proxy_http_version 1.1;

        # Required for WebSocket (/ws)
        proxy_set_header   Upgrade    $http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host       $host;

        # Required for streaming (/stream/*)
        proxy_buffering    off;
        proxy_read_timeout 3600s;
    }
}
```

---

## Running as a systemd service

```ini
# /etc/systemd/system/superkaraoke.service
[Unit]
Description=SuperKaraoke
After=network.target

[Service]
User=pi
WorkingDirectory=/opt/superkaraoke
EnvironmentFile=/opt/superkaraoke/.env
ExecStart=/opt/superkaraoke/.venv/bin/python run.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now superkaraoke
sudo journalctl -u superkaraoke -f
```

---

## Docker

### Build and run with Docker

```bash
# Build the image (frontend is compiled inside the build stage)
docker build -t superkaraoke .

# Run, mounting your karaoke directory and a volume for the database
docker run -d \
  --name superkaraoke \
  -p 8080:8080 \
  -v /path/to/your/karaoke:/media/karaoke \
  -v superkaraoke_data:/data \
  --restart unless-stopped \
  superkaraoke
```

Open `http://localhost:8080`.

### Docker Compose

Copy `docker-compose.yml` from the repo and edit the media path:

```yaml
services:
  superkaraoke:
    build: .
    ports:
      - "8080:8080"
    volumes:
      - /path/to/your/karaoke:/media/karaoke  # ← change this
      - superkaraoke_data:/data
    environment:
      SK_MEDIA_DIR: /media/karaoke
      SK_DB_PATH: /data/superkaraoke.db
    restart: unless-stopped

volumes:
  superkaraoke_data:
```

Then:

```bash
docker compose up -d          # start in background
docker compose logs -f        # follow logs
docker compose down           # stop
docker compose pull && docker compose up -d   # update to latest image
```

### Volume reference

| Mount point | Purpose |
|---|---|
| `/media/karaoke` | Karaoke media files (CDG+MP3 pairs, video files). Must be read-write for MusicBrainz apply (file rename), MP4 conversion, and YouTube downloads. Add `:ro` only if you disable all library-editing features. |
| `/data` | SQLite database (`superkaraoke.db`). Use a named volume or bind-mount a directory here so the library and like counts survive container restarts and image updates. |

### Environment variables in Docker

Pass any `SK_*` variable via `-e` or the `environment:` block in Compose:

```bash
docker run -d \
  -p 8080:8080 \
  -v /mnt/nas/karaoke:/media/karaoke \
  -v superkaraoke_data:/data \
  -e SK_FFMPEG_LOGLEVEL=info \
  superkaraoke
```

---

## Batch media conversion

The `convert_media.py` script permanently converts non-MP4 files in the library to browser-native H.264/AAC MP4. Converted files are served directly via range-request `FileResponse` — no runtime ffmpeg transcoding, no stalls, full seeking support.

### Supported conversions

| Type | Input | Output | Notes |
|---|---|---|---|
| `avi` `mkv` `mov` `wmv` `flv` `m4v` `mpg` `mpeg` `ts` `vob` | Any video file ffmpeg can decode | `.mp4` (H.264/AAC) | Source file removed after conversion |
| `cdg` | `.mp3` + `.cdg` pair | `.mp4` (H.264/AAC) | Both source files removed; DB record updated from kind `cdg` → `video` |
| `video` | All video formats above | `.mp4` | Shorthand alias |
| `all` | Everything | `.mp4` | Default when `--types` is omitted |

Output files use `-movflags +faststart` (moov atom at the front) for instant browser seeking.

**Safe to re-run** — if the target `.mp4` already exists the transcode step is skipped and only the database record is verified.

### Running locally

```bash
# Preview everything that would be converted (no changes made)
python3 library_scripts/convert_media.py --dry-run --media-dir /media/karaoke

# Convert everything (all video formats + CDG pairs)
python3 library_scripts/convert_media.py --media-dir /media/karaoke

# Convert only AVI and MKV files
python3 library_scripts/convert_media.py --types avi mkv --media-dir /media/karaoke

# Convert only CDG+MP3 pairs
python3 library_scripts/convert_media.py --types cdg --media-dir /media/karaoke

# Override the database path
python3 library_scripts/convert_media.py \
  --media-dir /media/karaoke \
  --db /var/lib/superkaraoke/superkaraoke.db
```

The server does **not** need to be stopped — files not yet converted continue to stream via the on-the-fly broadcaster while conversion runs in the background. After the script completes, click **Rescan** in the library UI (or restart the server) to confirm all paths are current.

### Running with Docker

Mount the same volumes as the running container so the script can reach the media files and the database:

```bash
# Convert everything
docker run --rm \
  -v /path/to/your/karaoke:/media/karaoke \
  -v superkaraoke_data:/data \
  superkaraoke \
  python3 library_scripts/convert_media.py \
    --media-dir /media/karaoke \
    --db /data/superkaraoke.db

# Convert only AVI files
docker run --rm \
  -v /path/to/your/karaoke:/media/karaoke \
  -v superkaraoke_data:/data \
  superkaraoke \
  python3 library_scripts/convert_media.py \
    --types avi \
    --media-dir /media/karaoke \
    --db /data/superkaraoke.db

# Dry run (media can stay read-only)
docker run --rm \
  -v /path/to/your/karaoke:/media/karaoke:ro \
  -v superkaraoke_data:/data \
  superkaraoke \
  python3 library_scripts/convert_media.py \
    --dry-run \
    --media-dir /media/karaoke \
    --db /data/superkaraoke.db
```

> **Note:** The media volume must be mounted **read-write** (without `:ro`) for actual conversions so the script can write the `.mp4` files and remove the originals.

### CLI reference

```
python3 library_scripts/convert_media.py [--types TYPE [TYPE ...]] [--dry-run] [--media-dir PATH] [--db PATH]

  --types TYPE ...   One or more of: avi, mkv, mov, wmv, flv, m4v, mpg, mpeg, ts, vob,
                     cdg, video (all video formats), all (default: all types)
  --media-dir PATH   Root of the karaoke media directory (default: /media/karaoke)
  --db PATH          Path to superkaraoke.db (default: /data/superkaraoke.db)
  --dry-run          Print what would be done without modifying any files or the database
```

---

## MusicBrainz metadata fix

The `library_scripts/mb_fix.py` script enriches song metadata (title, artist, year, genre) via the MusicBrainz database. It renames files to the `Artist - Title.ext` convention and updates the database.

### Running locally

```bash
# Interactive — review each match before applying
python3 library_scripts/mb_fix.py

# Automatic — apply only score=100 matches without prompting
python3 library_scripts/mb_fix.py --auto

# Only songs that have no artist set
python3 library_scripts/mb_fix.py --no-artist --auto

# Preview without changing anything
python3 library_scripts/mb_fix.py --auto --dry-run
```

### Running with Docker

```bash
# Interactive session (allocate a TTY)
docker run --rm -it \
  -v /path/to/your/karaoke:/media/karaoke \
  -v superkaraoke_data:/data \
  superkaraoke \
  python3 library_scripts/mb_fix.py

# Automatic — apply score=100 matches
docker run --rm \
  -v /path/to/your/karaoke:/media/karaoke \
  -v superkaraoke_data:/data \
  superkaraoke \
  python3 library_scripts/mb_fix.py --auto

# Dry run — preview without touching files or database
docker run --rm \
  -v /path/to/your/karaoke:/media/karaoke \
  -v superkaraoke_data:/data \
  superkaraoke \
  python3 library_scripts/mb_fix.py --auto --dry-run
```

### CLI reference

```
python3 library_scripts/mb_fix.py [options]

  --no-artist        Only process songs with no artist set (default: all songs)
  --youtube-only     Only process songs whose filename ends with a YouTube video ID
  --auto             Automatic mode: apply best result without prompting
  --min-score N      Minimum MusicBrainz score to accept in auto mode (default: 100)
  --delete-unmatched Auto mode: delete file and DB entry when no match meets the threshold
  --dry-run          Show what would change without renaming files or updating the DB
  --limit N          Stop after processing N songs
  --offset N         Skip the first N songs (useful for resuming)
  --db PATH          Path to superkaraoke.db (default: /data/superkaraoke.db)
  --media-dir PATH   Media root directory (default: /media/karaoke)
```

---

## Database path migration

The `library_scripts/path_replace.py` script replaces a path prefix in every `file_path` and `cdg_path` stored in the database.  Use it when moving a library to a machine where the media directory is mounted at a different location.

Song IDs are derived from the path relative to the media root (`sha256(relative_path)[:12]`), so when only the mount-point prefix changes the relative paths — and therefore the IDs — stay the same.  If the relative structure also changes, IDs are recomputed against the new media root.

### Running locally

```bash
# Preview what would change
python3 library_scripts/path_replace.py /mnt/nas/karaoke /media/karaoke --dry-run

# Apply the substitution
python3 library_scripts/path_replace.py /mnt/nas/karaoke /media/karaoke

# Specify a non-default database or media directory
python3 library_scripts/path_replace.py /old/path /new/path \
    --db /data/superkaraoke.db \
    --media-dir /media/karaoke
```

### Running with Docker

```bash
# Preview (media does not need to be mounted for a dry run)
docker run --rm \
  -v superkaraoke_data:/data \
  superkaraoke \
  python3 library_scripts/path_replace.py /mnt/nas/karaoke /media/karaoke --dry-run

# Apply the substitution
docker run --rm \
  -v superkaraoke_data:/data \
  superkaraoke \
  python3 library_scripts/path_replace.py /mnt/nas/karaoke /media/karaoke
```

After running, click **Rescan** in the library UI (or restart the container) to confirm all paths resolve correctly.

### CLI reference

```
python3 library_scripts/path_replace.py OLD_PREFIX NEW_PREFIX [options]

  OLD_PREFIX         Path prefix to replace (e.g. /mnt/nas/karaoke)
  NEW_PREFIX         Replacement prefix     (e.g. /media/karaoke)
  --db PATH          Path to superkaraoke.db (default: /data/superkaraoke.db)
  --media-dir PATH   New media root, used to recompute song IDs (default: /media/karaoke)
  --dry-run          Show what would change without modifying the database
```

---

## Sunfly catalogue match

The `library_scripts/sunfly_match.py` script matches Sun Fly CDG/MP3 files to the official Sunfly PDF catalogue, renames them to the correct `Artist - Title` format, and updates the database.

### Running locally

```bash
# Preview matches without making changes
python3 library_scripts/sunfly_match.py --dry-run \
  --pdf "/path/to/Sunfly karaoke list.pdf"

# Apply matches
python3 library_scripts/sunfly_match.py \
  --pdf "/path/to/Sunfly karaoke list.pdf"
```

### Running with Docker

Copy the Sunfly PDF into your `/data` volume first, then:

```bash
# Dry run — preview matches
docker run --rm \
  -v /path/to/your/karaoke:/media/karaoke \
  -v superkaraoke_data:/data \
  superkaraoke \
  python3 library_scripts/sunfly_match.py --dry-run

# Apply matches (PDF must be at /data/Sunfly karaoke list.pdf)
docker run --rm \
  -v /path/to/your/karaoke:/media/karaoke \
  -v superkaraoke_data:/data \
  superkaraoke \
  python3 library_scripts/sunfly_match.py

# Only specific albums
docker run --rm \
  -v /path/to/your/karaoke:/media/karaoke \
  -v superkaraoke_data:/data \
  superkaraoke \
  python3 library_scripts/sunfly_match.py --albums 52 100 114 --dry-run
```

### CLI reference

```
python3 library_scripts/sunfly_match.py [options]

  --pdf PATH         Path to the Sunfly karaoke PDF (default: /data/Sunfly karaoke list.pdf)
  --rebuild-cache    Re-parse the PDF even if a cache file exists
  --db PATH          Path to superkaraoke.db (default: /data/superkaraoke.db)
  --media-dir PATH   Media root directory (default: /media/karaoke)
  --dry-run          Show matches without renaming files or updating the database
  --albums N ...     Only process these SF album numbers (e.g. --albums 52 100 114)
  --fuzzy-threshold  Minimum fuzzy-match score 0–100 to accept (default: 70)
```

---

## Architecture notes

### Single-port streaming

ffmpeg is invoked with `pipe:1` as the output target, writing fragmented MP4 (`frag_keyframe+empty_moov+default_base_moof`) to stdout. FastAPI reads this in chunks and yields them via `StreamingResponse`. The result is a live HTTP stream over the same port as the web UI — no second port, no separate ffmpeg HTTP server.

### Multi-screen synchronization

When a song is dequeued:
1. One ffmpeg process starts, outputting to a `StreamBroadcaster`.
2. The broadcaster holds a list of `asyncio.Queue` objects, one per connected screen.
3. Each chunk from ffmpeg stdout is pushed to every subscriber queue simultaneously.
4. Each `/stream/{id}` response reads from its own queue — they all receive the exact same bytes in the same order.
5. A WebSocket `play` message fires to all screens at once, causing them to request the stream URL simultaneously, subscribing near position 0.

Synchronization is a property of the shared byte stream, not clock negotiation.

### Pitch shifting

Screens at semitones=0 share the default broadcaster. A screen requesting `?semitones=3` gets `get_or_start_stream(song, semitones=3)`, which spawns a separate ffmpeg process with `-af rubberband=pitch=1.189`. The `StreamManager` is keyed by `(song_id, semitones)`. When a song ends, `stop_all_for_song()` terminates every variant.

---

## Development

### Frontend hot-reload

```bash
# Terminal 1 — backend
SK_MEDIA_DIR=/path/to/karaoke .venv/bin/python run.py --reload

# Terminal 2 — frontend dev server (proxies API/WS to backend)
cd frontend
npm run dev
```

Open `http://localhost:5173` for the Vite dev server with HMR.

### Project layout

```
superkaraoke/
├── server/
│   ├── main.py            # FastAPI app, lifespan, route mounting
│   ├── config.py          # Pydantic settings (SK_* env vars)
│   ├── database.py        # SQLite schema, song CRUD, search
│   ├── library.py         # Filesystem scan, DB sync, file watcher
│   ├── metadata.py        # mutagen tags, filename parsing, MusicBrainz
│   ├── stream_manager.py  # ffmpeg broadcaster, per-semitone keying
│   ├── queue_manager.py   # Playback loop, queue mutations
│   ├── ws_manager.py      # WebSocket connection registry, broadcast
│   └── routers/
│       ├── songs.py       # GET /api/songs, POST /api/songs/{id}/like
│       ├── queue.py       # GET/POST/DELETE /api/queue
│       ├── library.py     # GET/PATCH /api/library, MusicBrainz lookup
│       ├── stream.py      # GET /stream/{id}?semitones=N
│       └── ws.py          # WS /ws
├── frontend/
│   ├── index.html         # User interface (queue + search)
│   ├── screen.html        # Display screen (fullscreen video)
│   ├── library.html       # Library management
│   ├── src/
│   │   ├── main.js        # Alpine.js: queue UI
│   │   ├── screen.js      # Alpine.js: display screen + pitch control
│   │   ├── library.js     # Alpine.js: library management
│   │   └── style.css      # Tailwind CSS directives + component classes
│   ├── tailwind.config.js
│   └── vite.config.js
├── library_scripts/
│   ├── convert_media.py   # Batch media conversion utility (video + CDG → MP4)
│   ├── mb_fix.py          # MusicBrainz metadata enrichment + file rename
│   ├── path_replace.py    # Replace file_path prefix in DB (for instance migration)
│   └── sunfly_match.py    # Sunfly catalogue matching + file rename
├── run.py                 # Entry point (uvicorn)
└── requirements.txt
```

---

## License

MIT
