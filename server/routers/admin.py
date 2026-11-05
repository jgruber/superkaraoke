"""
Admin endpoints for in-app maintenance:

GET  /api/admin/versions          — current yt-dlp, bgutil plugin, PO-token sidecar
POST /api/admin/yt-dlp/update     — pip install -U yt-dlp + bgutil plugin, restart self
POST /api/admin/po-token/update   — pull + redeploy the bgutil-pot sidecar via Portainer

Cross-container operations (self-restart, sidecar re-pull) go through the
Portainer API. The superkaraoke container must be given four env vars for
these endpoints to work (otherwise they 503 with a clear message):
  SK_PORTAINER_URL         e.g. https://containers.grubernet.org
  SK_PORTAINER_TOKEN       X-API-Key
  SK_PORTAINER_ENDPOINT_ID numeric endpoint id for this host
  SK_PORTAINER_STACK_ID    numeric id of the superkaraoke stack
"""
import asyncio
import logging
import os
import sys
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

_SELF_CONTAINER   = os.environ.get("SK_SELF_CONTAINER_NAME", "superkaraoke")
_BGUTIL_CONTAINER = os.environ.get("SK_BGUTIL_CONTAINER_NAME", "superkaraoke-bgutil-pot")
_BGUTIL_PING_URL  = os.environ.get("GETPOT_BGUTIL_BASE_URL", "http://bgutil-pot:4416").rstrip("/") + "/ping"

_pip_lock = asyncio.Lock()


# ── Version helpers ──────────────────────────────────────────────────────────

def _ytdlp_version() -> str:
    try:
        import yt_dlp  # noqa: PLC0415
        return getattr(yt_dlp.version, "__version__", "unknown")
    except Exception:
        return "unknown"


def _pkg_version_safe(name: str) -> str:
    try:
        return _pkg_version(name)
    except PackageNotFoundError:
        return "not-installed"


async def _potoken_sidecar_info() -> dict:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get(_BGUTIL_PING_URL)
            if r.status_code == 200:
                return {"ok": True, **r.json()}
            return {"ok": False, "status": r.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Portainer glue ───────────────────────────────────────────────────────────

class _PortainerCfg:
    def __init__(self):
        self.url          = (os.environ.get("SK_PORTAINER_URL") or "").rstrip("/")
        self.token        = os.environ.get("SK_PORTAINER_TOKEN") or ""
        self.endpoint_id  = os.environ.get("SK_PORTAINER_ENDPOINT_ID") or ""
        self.stack_id     = os.environ.get("SK_PORTAINER_STACK_ID") or ""

    @property
    def ready(self) -> bool:
        return bool(self.url and self.token and self.endpoint_id and self.stack_id)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.url,
            headers={"X-API-Key": self.token},
            verify=False,     # tolerate self-signed certs on home-lab Portainer
            timeout=120.0,
        )


def _require_portainer() -> _PortainerCfg:
    cfg = _PortainerCfg()
    if not cfg.ready:
        raise HTTPException(
            status_code=503,
            detail=(
                "Portainer not configured — set SK_PORTAINER_URL, "
                "SK_PORTAINER_TOKEN, SK_PORTAINER_ENDPOINT_ID, "
                "SK_PORTAINER_STACK_ID on this container."
            ),
        )
    return cfg


async def _restart_container_via_portainer(cfg: _PortainerCfg, name: str) -> None:
    async with cfg.client() as c:
        r = await c.post(
            f"/api/endpoints/{cfg.endpoint_id}/docker/containers/{name}/restart",
            params={"t": 5},
        )
        if r.status_code not in (204, 304):
            log.warning("Portainer restart(%s) returned %s: %s",
                        name, r.status_code, r.text[:200])


async def _redeploy_stack_pull(cfg: _PortainerCfg) -> dict:
    async with cfg.client() as c:
        r = await c.get(f"/api/stacks/{cfg.stack_id}/file")
        r.raise_for_status()
        content = r.json().get("StackFileContent", "")
        if not content:
            raise HTTPException(500, "Portainer stack file is empty")
        r = await c.put(
            f"/api/stacks/{cfg.stack_id}",
            params={"endpointId": cfg.endpoint_id},
            json={
                "stackFileContent": content,
                "env":              [],
                "prune":            False,
                "pullImage":        True,
            },
        )
        if r.status_code >= 300:
            raise HTTPException(500, f"Portainer redeploy failed: {r.status_code} {r.text[:200]}")
        return r.json()


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/versions")
async def versions():
    return {
        "yt_dlp":           _ytdlp_version(),
        "bgutil_plugin":    _pkg_version_safe("bgutil-ytdlp-pot-provider"),
        "po_token_sidecar": await _potoken_sidecar_info(),
        "portainer_ready":  _PortainerCfg().ready,
    }


class UpdateYtdlpBody(BaseModel):
    restart: bool = True


async def _inspect_installed() -> tuple[str, str]:
    """Spawn a child Python to read the installed-but-not-yet-loaded versions."""
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c",
        "import yt_dlp, importlib.metadata as m;"
        "print(yt_dlp.version.__version__);"
        "print(m.version('bgutil-ytdlp-pot-provider'))",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await child.communicate()
    lines = out.decode(errors="replace").strip().splitlines()
    yt = lines[0] if len(lines) >= 1 else "unknown"
    bg = lines[1] if len(lines) >= 2 else "unknown"
    return yt, bg


@router.post("/yt-dlp/update")
async def update_ytdlp(body: UpdateYtdlpBody):
    if _pip_lock.locked():
        raise HTTPException(409, "Another update is already running")
    async with _pip_lock:
        before_yt = _ytdlp_version()
        before_bg = _pkg_version_safe("bgutil-ytdlp-pot-provider")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", "--no-cache-dir", "-U",
            "yt-dlp", "bgutil-ytdlp-pot-provider",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            raise HTTPException(
                500, detail={"pip_exit": proc.returncode,
                             "log": stdout.decode(errors="replace")[-4000:]},
            )
        after_yt, after_bg = await _inspect_installed()

    result = {
        "before":        {"yt_dlp": before_yt, "bgutil_plugin": before_bg},
        "after":         {"yt_dlp": after_yt,  "bgutil_plugin": after_bg},
        "needs_restart": (after_yt != before_yt or after_bg != before_bg),
    }

    if body.restart and result["needs_restart"]:
        try:
            cfg = _require_portainer()
        except HTTPException as e:
            result["restart_failed"] = e.detail
            return result
        async def _bg_restart():
            await asyncio.sleep(0.4)   # let the HTTP response flush first
            try:
                await _restart_container_via_portainer(cfg, _SELF_CONTAINER)
            except Exception as exc:
                log.exception("self-restart via Portainer failed: %s", exc)
        asyncio.create_task(_bg_restart())
        result["restarting"] = True
    return result


@router.post("/po-token/update")
async def update_po_token():
    """
    Pull the latest bgutil-pot sidecar image and recreate the container by
    redeploying the stack with pullImage=true. The superkaraoke container
    survives unchanged (same image digest → same container).
    """
    cfg = _require_portainer()
    before = await _potoken_sidecar_info()
    out = await _redeploy_stack_pull(cfg)

    # Give the sidecar a few seconds to come back up before reporting
    after = {"ok": False, "error": "did not respond in time"}
    for _ in range(15):
        await asyncio.sleep(1)
        info = await _potoken_sidecar_info()
        if info.get("ok"):
            after = info
            break
    return {
        "redeployed":     True,
        "stack_name":     out.get("Name", "superkaraoke"),
        "before":         before,
        "after":          after,
    }
