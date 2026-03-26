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

Example `.env`:

```env
SK_MEDIA_DIR=/media/karaoke
SK_PORT=8080
SK_DB_PATH=/var/lib/superkaraoke/library.db
```

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
  -v /path/to/your/karaoke:/media/karaoke:ro \
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
      - /path/to/your/karaoke:/media/karaoke:ro  # ← change this
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
| `/media/karaoke` | Karaoke media files (CDG+MP3 pairs, video files). Mount read-only if you don't want the container writing to your library. |
| `/data` | SQLite database (`superkaraoke.db`). Use a named volume or bind-mount a directory here so the library and like counts survive container restarts and image updates. |

### Environment variables in Docker

Pass any `SK_*` variable via `-e` or the `environment:` block in Compose:

```bash
docker run -d \
  -p 8080:8080 \
  -v /mnt/nas/karaoke:/media/karaoke:ro \
  -v superkaraoke_data:/data \
  -e SK_FFMPEG_LOGLEVEL=info \
  superkaraoke
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
├── run.py                 # Entry point (uvicorn)
└── requirements.txt
```

---

## License

MIT
