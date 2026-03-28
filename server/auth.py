"""
Authentication utilities.

  • IP/network checking (CIDR-based local bypass)
  • PBKDF2-SHA256 password hashing (stdlib only, no extra deps)
  • In-memory session store with 30-day TTL
  • credentials.json CRUD (same directory as the SQLite database)

Bootstrap mode: if credentials.json has no users yet, every client is treated
as local so the operator can create the first user from any browser.
"""

import hashlib
import ipaddress
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Optional

from fastapi import Request, Response

log = logging.getLogger(__name__)

# ── Password hashing ──────────────────────────────────────────────────────────

_ITERATIONS = 260_000   # OWASP 2023 recommendation for PBKDF2-SHA256


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk   = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2:sha256:{_ITERATIONS}:{salt.hex()}:{dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, algo, iters, salt_hex, dk_hex = stored.split(":")
        salt = bytes.fromhex(salt_hex)
        dk   = hashlib.pbkdf2_hmac(algo, password.encode(), salt, int(iters))
        return secrets.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


# ── Network helpers ───────────────────────────────────────────────────────────

def parse_networks(nets_str: str) -> list:
    """Parse a comma-separated CIDR string into a list of ip_network objects."""
    networks = []
    for s in nets_str.split(","):
        s = s.strip()
        if not s:
            continue
        try:
            networks.append(ipaddress.ip_network(s, strict=False))
        except ValueError:
            log.warning("Invalid CIDR in ALLOWED_NETWORKS: %r — ignored", s)
    return networks


def _client_ip(request: Request) -> str:
    """
    Return the real client IP.

    Trusts X-Forwarded-For only when the direct TCP connection is from
    localhost — the common pattern when nginx sits on the same host.
    """
    direct = (request.client.host if request.client else "") or ""
    if direct in ("127.0.0.1", "::1", "localhost"):
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
    return direct


def is_local(request: Request, networks: list) -> bool:
    """Return True if the client IP falls within any of the allowed networks."""
    if not networks:
        return False
    ip_str = _client_ip(request)
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(addr in net for net in networks)


# ── Session store (in-memory) ─────────────────────────────────────────────────

_COOKIE    = "sk_session"
_TTL       = 30 * 24 * 3600   # 30 days
_sessions: dict[str, dict] = {}   # token → {username, created_at}


def create_session(response: Response, username: str) -> None:
    token = secrets.token_hex(32)
    _sessions[token] = {"username": username, "created_at": time.time()}
    response.set_cookie(
        _COOKIE, token,
        max_age=_TTL,
        httponly=True,
        samesite="lax",
    )


def get_session_user(request: Request) -> Optional[str]:
    token = request.cookies.get(_COOKIE)
    if not token:
        return None
    entry = _sessions.get(token)
    if not entry:
        return None
    if time.time() - entry["created_at"] > _TTL:
        del _sessions[token]
        return None
    return entry["username"]


def clear_session(request: Request, response: Response) -> None:
    token = request.cookies.get(_COOKIE)
    if token:
        _sessions.pop(token, None)
    response.delete_cookie(_COOKIE)


# ── Credentials file ──────────────────────────────────────────────────────────

def _creds_path(db_path: Path) -> Path:
    return db_path.parent / "credentials.json"


def _load(db_path: Path) -> dict:
    p = _creds_path(db_path)
    if not p.exists():
        return {"users": {}}
    try:
        return json.loads(p.read_text())
    except Exception as exc:
        log.error("Failed to load credentials.json: %s", exc)
        return {"users": {}}


def _save(db_path: Path, creds: dict) -> None:
    _creds_path(db_path).write_text(json.dumps(creds, indent=2))


def has_any_users(db_path: Path) -> bool:
    return bool(_load(db_path)["users"])


def list_users(db_path: Path) -> list[str]:
    return list(_load(db_path)["users"].keys())


def authenticate(db_path: Path, username: str, password: str) -> bool:
    user = _load(db_path)["users"].get(username)
    if not user:
        return False
    return verify_password(password, user["password_hash"])


def create_user(db_path: Path, username: str, password: str) -> bool:
    """Create user. Returns False if username already exists."""
    creds = _load(db_path)
    if username in creds["users"]:
        return False
    creds["users"][username] = {"password_hash": hash_password(password)}
    _save(db_path, creds)
    return True


def update_password(db_path: Path, username: str, new_password: str) -> bool:
    """Update password. Returns False if user not found."""
    creds = _load(db_path)
    if username not in creds["users"]:
        return False
    creds["users"][username]["password_hash"] = hash_password(new_password)
    _save(db_path, creds)
    return True


def delete_user(db_path: Path, username: str) -> bool:
    """Delete user. Returns False if user not found."""
    creds = _load(db_path)
    if username not in creds["users"]:
        return False
    del creds["users"][username]
    _save(db_path, creds)
    return True
