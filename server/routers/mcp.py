"""
HTTP MCP endpoint: JSON-RPC 2.0 over POST.

Auth: the logged-in user's 6-character agent key is sent as
``Authorization: Bearer <KEY>``. Keys are per-session, in-memory, and
displayed in the user's profile menu after login.

The MCP endpoint is scoped under ``/api/mcp`` and is already exempt from
the session-cookie middleware in main.py; auth happens here instead.
"""
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from ..auth import agent_key_user
from ..mcp_tools import dispatch, TOOLS

router = APIRouter(tags=["mcp"])
log = logging.getLogger(__name__)


def _require_agent_key(request: Request) -> str:
    """Return the username this request authenticates as, or raise 401."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer agent key")
    user = agent_key_user(auth[7:])
    if not user:
        raise HTTPException(status_code=401, detail="Invalid agent key")
    return user


@router.post("/mcp")
async def mcp_endpoint(request: Request):
    user = _require_agent_key(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}, "id": None},
        )

    # JSON-RPC 2.0 batch support
    if isinstance(body, list):
        responses: list = []
        for req in body:
            if not isinstance(req, dict):
                continue
            r = await dispatch(req)
            if r is not None:
                responses.append(r)
        if not responses:
            return Response(status_code=202)
        return JSONResponse(responses)

    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400,
            content={"jsonrpc": "2.0", "error": {"code": -32600, "message": "Invalid request"}, "id": None},
        )

    r = await dispatch(body)
    if r is None:
        return Response(status_code=202)
    log.debug("[mcp %s] %s → %s", user, body.get("method"), r.get("result") or r.get("error"))
    return JSONResponse(r)


@router.get("/mcp")
async def mcp_info(request: Request):
    """Confirm auth works and list tool names."""
    _require_agent_key(request)
    return {
        "protocolVersion": "2024-11-05",
        "transport": "http-jsonrpc",
        "tools": list(TOOLS.keys()),
    }
