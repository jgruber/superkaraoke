import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .auth import is_local, get_session_user, parse_networks, has_any_users
from .config import settings
from .database import init_db
from .library import library
from .stream_manager import stream_manager
from .queue_manager import queue_manager
from .routers import songs, queue, stream, ws, library as library_router, youtube as youtube_router
from .routers.auth import router as auth_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    from .database import count_songs
    library._song_count = await count_songs()
    library.start_watcher()
    queue_manager.start_loop()
    asyncio.create_task(library.scan())
    log.info("Server ready. Library scan running in background (%s)", settings.media_dir)
    yield
    library.stop_watcher()
    await stream_manager.shutdown()


app = FastAPI(title="SuperKaraoke", lifespan=lifespan)

# ── Auth middleware ────────────────────────────────────────────────────────────
# Protects all /api/ paths except /api/auth/* (login/logout/me are always open).
# HTML pages, static assets, WebSocket, and streams load without a cookie so
# that the JS can check auth on its own and show the login modal if needed.

_EXEMPT_PREFIXES = (
    "/api/auth/",   # login, logout, me — always open
    "/ws",          # WebSocket
    "/stream/",     # media streams
    "/health",
    "/assets/",
    "/favicon",
)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path

    # Non-API paths (HTML pages, static files) pass through — JS handles auth UI
    if not path.startswith("/api/"):
        return await call_next(request)

    # Exempt API paths (auth endpoints)
    if any(path.startswith(p) for p in _EXEMPT_PREFIXES):
        return await call_next(request)

    # Bootstrap mode: no users yet — let everything through so the operator
    # can reach the UI and create the first account
    if not has_any_users(settings.db_path):
        return await call_next(request)

    # Check local network or valid session
    networks = parse_networks(settings.allowed_networks)
    if is_local(request, networks) or get_session_user(request):
        return await call_next(request)

    return JSONResponse({"detail": "Not authenticated"}, status_code=401)


# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(auth_router,           prefix="/api")
app.include_router(songs.router,          prefix="/api")
app.include_router(queue.router,          prefix="/api")
app.include_router(library_router.router, prefix="/api")
app.include_router(youtube_router.router, prefix="/api")
app.include_router(stream.router)
app.include_router(ws.router)

# ── Static / HTML ─────────────────────────────────────────────────────────────

_static_dir = Path(__file__).parent.parent / settings.static_dir

if _static_dir.exists():
    app.mount("/assets", StaticFiles(directory=str(_static_dir / "assets")), name="assets")

    @app.get("/favicon.svg")
    async def favicon():
        return FileResponse(str(_static_dir / "favicon.svg"), media_type="image/svg+xml")

    @app.get("/")
    async def index():
        return FileResponse(str(_static_dir / "index.html"))

    @app.get("/screen")
    async def screen():
        return FileResponse(str(_static_dir / "screen.html"))

    @app.get("/library")
    async def library_page():
        return FileResponse(str(_static_dir / "library.html"))

else:
    @app.get("/")
    async def index_dev():
        return {"message": "Frontend not built. Run: cd frontend && npm run build"}

    @app.get("/screen")
    async def screen_dev():
        return {"message": "Frontend not built. Run: cd frontend && npm run build"}

    @app.get("/library")
    async def library_dev():
        return {"message": "Frontend not built. Run: cd frontend && npm run build"}


@app.get("/health")
async def health():
    return {"status": "ok", "songs": library.songs}
