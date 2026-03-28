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
    if not authenticate(settings.db_path, body.username.strip(), body.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    create_session(response, body.username.strip())
    return {"username": body.username.strip()}


@router.post("/auth/logout")
async def logout(request: Request, response: Response):
    clear_session(request, response)
    return {"ok": True}


@router.get("/auth/me")
async def me(request: Request):
    from ..auth import has_any_users
    networks = _networks()
    local    = is_local(request, networks)
    username = get_session_user(request)

    # Bootstrap mode: no users configured yet → treat everyone as local
    if not has_any_users(settings.db_path):
        return {"username": None, "local": True, "bootstrap": True}

    if not local and not username:
        raise HTTPException(status_code=401, detail="Not authenticated")

    return {"username": username, "local": local, "bootstrap": False}


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
