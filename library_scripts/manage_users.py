#!/usr/bin/env python3
"""
manage_users.py — Manage SuperKaraoke user accounts in credentials.json.

Passwords are hashed with PBKDF2-SHA256 (stdlib, no extra dependencies).
The credentials file lives in the same directory as the database.

Usage
─────
  # List all users
  python3 library_scripts/manage_users.py list

  # Add a user (prompts for password)
  python3 library_scripts/manage_users.py add alice

  # Add a user with password supplied directly (useful in scripts)
  python3 library_scripts/manage_users.py add alice --password s3cr3t

  # Change a user's password (prompts for new password)
  python3 library_scripts/manage_users.py password alice

  # Change password non-interactively
  python3 library_scripts/manage_users.py password alice --password newpass

  # Remove a user
  python3 library_scripts/manage_users.py remove alice

  # Specify a non-default credentials / database path
  python3 library_scripts/manage_users.py list --db /data/superkaraoke.db
"""

import argparse
import getpass
import hashlib
import json
import secrets
import sys
from pathlib import Path

# ── Defaults ──────────────────────────────────────────────────────────────────

_DEFAULT_DB = Path("/data/superkaraoke.db")

# ── Password hashing (must match server/auth.py) ──────────────────────────────

_ITERATIONS = 260_000


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk   = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2:sha256:{_ITERATIONS}:{salt.hex()}:{dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        _, algo, iters, salt_hex, dk_hex = stored.split(":")
        salt = bytes.fromhex(salt_hex)
        dk   = hashlib.pbkdf2_hmac(algo, password.encode(), salt, int(iters))
        return secrets.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


# ── Credentials file helpers ──────────────────────────────────────────────────

def _creds_path(db_path: Path) -> Path:
    return db_path.parent / "credentials.json"


def _load(db_path: Path) -> dict:
    p = _creds_path(db_path)
    if not p.exists():
        return {"users": {}}
    try:
        return json.loads(p.read_text())
    except Exception as exc:
        print(f"Error: could not read {p}: {exc}", file=sys.stderr)
        sys.exit(1)


def _save(db_path: Path, creds: dict) -> None:
    p = _creds_path(db_path)
    p.write_text(json.dumps(creds, indent=2))


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_list(db_path: Path) -> None:
    creds = _load(db_path)
    users = list(creds["users"].keys())
    if not users:
        print("No users configured.")
        return
    print(f"{len(users)} user{'s' if len(users) != 1 else ''}:")
    for u in sorted(users):
        print(f"  {u}")


def cmd_add(db_path: Path, username: str, password: str | None) -> None:
    creds = _load(db_path)
    if username in creds["users"]:
        print(f"Error: user '{username}' already exists.", file=sys.stderr)
        sys.exit(1)

    if password is None:
        password = _prompt_new_password(username)

    creds["users"][username] = {"password_hash": _hash_password(password)}
    _save(db_path, creds)
    print(f"User '{username}' created.")


def cmd_password(db_path: Path, username: str, password: str | None) -> None:
    creds = _load(db_path)
    if username not in creds["users"]:
        print(f"Error: user '{username}' not found.", file=sys.stderr)
        sys.exit(1)

    if password is None:
        password = _prompt_new_password(username)

    creds["users"][username]["password_hash"] = _hash_password(password)
    _save(db_path, creds)
    print(f"Password updated for '{username}'.")


def cmd_remove(db_path: Path, username: str) -> None:
    creds = _load(db_path)
    if username not in creds["users"]:
        print(f"Error: user '{username}' not found.", file=sys.stderr)
        sys.exit(1)

    try:
        confirm = input(f"Remove user '{username}'? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        sys.exit(0)

    if confirm != "y":
        print("Aborted.")
        sys.exit(0)

    del creds["users"][username]
    _save(db_path, creds)
    print(f"User '{username}' removed.")


# ── Prompt helpers ────────────────────────────────────────────────────────────

def _prompt_new_password(username: str) -> str:
    while True:
        try:
            pw  = getpass.getpass(f"New password for '{username}': ")
            pw2 = getpass.getpass("Confirm password: ")
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(0)
        if not pw:
            print("Password cannot be empty.")
            continue
        if pw != pw2:
            print("Passwords do not match. Try again.")
            continue
        return pw


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", type=Path, default=_DEFAULT_DB,
                        help=f"Path to superkaraoke.db (default: {_DEFAULT_DB})")

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    sub.add_parser("list", help="List all users")

    p_add = sub.add_parser("add", help="Add a new user")
    p_add.add_argument("username")
    p_add.add_argument("--password", default=None,
                       help="Password (prompted interactively if omitted)")

    p_pw = sub.add_parser("password", help="Change a user's password")
    p_pw.add_argument("username")
    p_pw.add_argument("--password", default=None,
                      help="New password (prompted interactively if omitted)")

    p_rm = sub.add_parser("remove", help="Remove a user")
    p_rm.add_argument("username")

    args = parser.parse_args()

    # Validate DB parent directory exists (credentials.json will live there)
    if not args.db.parent.exists():
        print(f"Error: directory {args.db.parent} does not exist.", file=sys.stderr)
        sys.exit(1)

    if args.command == "list":
        cmd_list(args.db)
    elif args.command == "add":
        cmd_add(args.db, args.username, args.password)
    elif args.command == "password":
        cmd_password(args.db, args.username, args.password)
    elif args.command == "remove":
        cmd_remove(args.db, args.username)


if __name__ == "__main__":
    main()
