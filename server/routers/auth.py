"""
Authentication and user management endpoints.

POST /api/auth/login                    — log in, set session cookie
POST /api/auth/logout                   — clear session cookie
GET  /api/auth/me                       — current user info

GET  /api/users                         — list usernames
POST /api/users                         — create user
POST /api/users/{username}/password     — change password
DELETE /api/users/{username}            — delete user
"""
import logging
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
from typing import Optional

from ..auth import (
    authenticate, create_user, update_password, delete_user, list_users,
    create_session, clear_session, get_session_user, is_local, parse_networks,
    _client_ip, ensure_agent_key, rotate_agent_key, revoke_agent_key,
)
from ..config import settings

log = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _networks():
    return parse_networks(settings.allowed_networks)


def _check_access(request: Request) -> None:
    """Raise 403 if neither local nor authenticated."""
    if is_local(request, _networks()) or get_session_user(request):
        return
    raise HTTPException(status_code=403, detail="Authentication required")


# ── Auth ──────────────────────────────────────────────────────────────────────

class LoginBody(BaseModel):
    username: str
    password: str


@router.post("/auth/login")
async def login(body: LoginBody, response: Response):
    username = body.username.strip()
    if not authenticate(settings.db_path, username, body.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    create_session(response, username)
    return {"username": username, "agent_key": ensure_agent_key(username)}


@router.post("/auth/logout")
async def logout(request: Request, response: Response):
    username = get_session_user(request)
    if username:
        revoke_agent_key(username)
    clear_session(request, response)
    return {"ok": True}


def _agent_key_identity(request: Request) -> Optional[str]:
    """
    Return the identity string we mint an agent key under:
      - real logged-in users → their username
      - local-bypass users   → "local:<client-ip>"
      - everyone else        → None (no key)
    """
    username = get_session_user(request)
    if username:
        return username
    if is_local(request, _networks()):
        ip = _client_ip(request)
        if ip:
            return f"local:{ip}"
    return None


@router.get("/auth/me")
async def me(request: Request):
    from ..auth import has_any_users
    networks = _networks()
    local    = is_local(request, networks)
    username = get_session_user(request)
    client_ip = _client_ip(request)

    # Bootstrap mode: no users configured yet → treat everyone as local
    if not has_any_users(settings.db_path):
        bootstrap_identity = f"local:{client_ip}" if client_ip else None
        return {
            "username": None,
            "local": True,
            "bootstrap": True,
            "client_ip": client_ip,
            "allowed_networks": settings.allowed_networks,
            "agent_key": ensure_agent_key(bootstrap_identity) if bootstrap_identity else None,
        }

    if not local and not username:
        raise HTTPException(status_code=401, detail="Not authenticated")

    identity = _agent_key_identity(request)
    return {
        "username": username,
        "local": local,
        "bootstrap": False,
        "client_ip": client_ip,
        "allowed_networks": settings.allowed_networks,
        "agent_key": ensure_agent_key(identity) if identity else None,
    }


# ── Agent key (per-identity bearer token for the MCP endpoint) ───────────────

@router.post("/auth/agent-key/rotate")
async def rotate_my_agent_key(request: Request):
    identity = _agent_key_identity(request)
    if not identity:
        raise HTTPException(status_code=401, detail="Login or local access required")
    return {"agent_key": rotate_agent_key(identity)}


# ── User management ───────────────────────────────────────────────────────────

class CreateUserBody(BaseModel):
    username: str
    password: str


class ChangePasswordBody(BaseModel):
    current_password: Optional[str] = None
    new_password: str


@router.get("/users")
async def get_users(request: Request):
    _check_access(request)
    return {"users": list_users(settings.db_path)}


@router.post("/users")
async def add_user(body: CreateUserBody, request: Request):
    _check_access(request)
    username = body.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username cannot be empty")
    if not body.password:
        raise HTTPException(status_code=400, detail="Password cannot be empty")
    if not create_user(settings.db_path, username, body.password):
        raise HTTPException(status_code=409, detail="Username already exists")
    log.info("User created: %s", username)
    return {"username": username}


@router.post("/users/{username}/password")
async def change_password(username: str, body: ChangePasswordBody, request: Request):
    _check_access(request)
    networks     = _networks()
    session_user = get_session_user(request)
    local        = is_local(request, networks)

    # Remote authenticated users may only change their own password,
    # and must supply the current one to prove possession.
    if not local:
        if session_user != username:
            raise HTTPException(status_code=403, detail="Cannot change another user's password")
        if not body.current_password or not authenticate(settings.db_path, username, body.current_password):
            raise HTTPException(status_code=401, detail="Current password is incorrect")

    if not body.new_password:
        raise HTTPException(status_code=400, detail="New password cannot be empty")
    if not update_password(settings.db_path, username, body.new_password):
        raise HTTPException(status_code=404, detail="User not found")
    log.info("Password changed for: %s", username)
    return {"ok": True}


@router.delete("/users/{username}")
async def remove_user(username: str, request: Request):
    _check_access(request)
    if not delete_user(settings.db_path, username):
        raise HTTPException(status_code=404, detail="User not found")
    log.info("User deleted: %s", username)
    return {"ok": True}
