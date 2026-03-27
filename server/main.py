import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from .config import settings
from .database import init_db
from .library import library
from .stream_manager import stream_manager
from .queue_manager import queue_manager
from .routers import songs, queue, stream, ws, library as library_router, youtube as youtube_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # Seed count from DB immediately (may be 0 on first run)
    from .database import count_songs
    library._song_count = await count_songs()
    library.start_watcher()
    queue_manager.start_loop()
    # Run scan in background — server is available while library indexes
    asyncio.create_task(library.scan())
    log.info("Server ready. Library scan running in background (%s)", settings.media_dir)
    yield
    library.stop_watcher()
    await stream_manager.shutdown()


app = FastAPI(title="SuperKaraoke", lifespan=lifespan)

app.include_router(songs.router,          prefix="/api")
app.include_router(queue.router,          prefix="/api")
app.include_router(library_router.router, prefix="/api")
app.include_router(youtube_router.router, prefix="/api")
app.include_router(stream.router)
app.include_router(ws.router)

# Serve built frontend — graceful fallback when not yet built
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
