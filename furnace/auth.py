"""API tokens, the wrong-token lockout and the EDL logins.

API tokens and EDL logins live in the database, so the replication copies
them to the other server. Only a hash is stored. A token or a password is
shown once, when it is made. Lost? Make a new one with "rotate".

API tokens: SHA-256 of a long random token. A token has a name (who uses it,
for example "soar" or "ui") and a scope: block (IPs and domains), whitelist, or all.

EDL logins: nginx basic auth. The password hash is SHA-512 crypt ($6$),
made by openssl. nginx reads the users file on every request, so a change
works without a reload.

Lockout: a caller IP that sends too many wrong tokens is locked for a while.
It is kept in its own small database on each server and is not replicated.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import subprocess
import time
from dataclasses import dataclass

from . import db
from .config import Settings

SCOPES = ("block", "whitelist", "all")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
TOKEN_PREFIX = "fur_"


class AuthError(Exception):
    """A token or login command cannot be done. The message says why."""


@dataclass
class Client:
    name: str
    scope: str

    def may(self, kind: str) -> bool:
        return self.scope == "all" or self.scope == kind


def check_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise AuthError("A name has 1 to 40 characters: letters, numbers, dot, dash or underscore. "
                        "It starts with a letter or a number.")
    return name


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def new_password() -> str:
    return secrets.token_urlsafe(24)


# ---------------------------------------------------------------------- API tokens

def find_client(settings: Settings, token: str) -> Client | None:
    """The client that owns this token, or None."""
    if not token:
        return None
    selftest = settings.selftest_token()
    if selftest and hmac.compare_digest(token.encode("utf-8"), selftest.encode("utf-8")):
        return Client("setup-selftest", "all")
    conn = db.connect(settings.db_path)
    try:
        row = conn.execute("SELECT name, scope FROM api_tokens WHERE token_hash = ?",
                           (hash_token(token),)).fetchone()
    except sqlite3.OperationalError:  # a snapshot from an older version without the table
        return None
    finally:
        conn.close()
    return Client(row["name"], row["scope"]) if row else None


def list_tokens(conn: sqlite3.Connection) -> list[dict[str, str]]:
    return [{"name": r["name"], "scope": r["scope"], "created_at": r["created_at"],
             "created_by": r["created_by"], "id": r["token_hash"][:8]}
            for r in conn.execute("SELECT * FROM api_tokens ORDER BY name")]


def add_token(conn: sqlite3.Connection, name: str, scope: str, actor: str, token: str | None = None) -> str:
    name = check_name(name)
    if scope not in SCOPES:
        raise AuthError(f"The scope must be one of: {', '.join(SCOPES)}.")
    token = token or new_token()
    with db.transaction(conn):
        if conn.execute("SELECT 1 FROM api_tokens WHERE name = ?", (name,)).fetchone():
            raise AuthError(f"A token named '{name}' exists already. Use 'token rotate {name}' for a new one.")
        conn.execute("INSERT INTO api_tokens(name, token_hash, scope, created_at, created_by) VALUES (?,?,?,?,?)",
                     (name, hash_token(token), scope, db.to_iso(db.utcnow()), actor))
        db.audit(conn, "token_add", "api_token", name, "ok", actor=actor, source="cli", details=f"scope={scope}")
    return token


def rotate_token(conn: sqlite3.Connection, name: str, actor: str) -> str:
    name = check_name(name)
    token = new_token()
    with db.transaction(conn):
        changed = conn.execute("UPDATE api_tokens SET token_hash = ?, created_at = ?, created_by = ? WHERE name = ?",
                               (hash_token(token), db.to_iso(db.utcnow()), actor, name)).rowcount
        if not changed:
            raise AuthError(f"There is no token named '{name}'. See: furnace token list")
        db.audit(conn, "token_rotate", "api_token", name, "ok", actor=actor, source="cli")
    return token


def remove_token(conn: sqlite3.Connection, name: str, actor: str) -> None:
    name = check_name(name)
    with db.transaction(conn):
        if not conn.execute("DELETE FROM api_tokens WHERE name = ?", (name,)).rowcount:
            raise AuthError(f"There is no token named '{name}'. See: furnace token list")
        db.audit(conn, "token_remove", "api_token", name, "ok", actor=actor, source="cli")


def import_legacy_tokens(conn: sqlite3.Connection, values: dict[str, str]) -> list[str]:
    """Version 0.2 kept two tokens in tokens.env. Keep them working under names.

    Only when the database has no tokens yet. Returns the names that were added.
    """
    if conn.execute("SELECT COUNT(*) FROM api_tokens").fetchone()[0]:
        return []
    block = values.get("FURNACE_BLOCK_TOKEN", "").strip()
    whitelist = values.get("FURNACE_WHITELIST_TOKEN", "").strip()
    added: list[str] = []
    if block:
        add_token(conn, "block", "block" if whitelist and whitelist != block else "all", "upgrade", token=block)
        added.append("block")
    if whitelist and whitelist != block:
        add_token(conn, "whitelist", "whitelist", "upgrade", token=whitelist)
        added.append("whitelist")
    return added


# ---------------------------------------------------------------------- EDL logins

def hash_password(password: str) -> str:
    """SHA-512 crypt ($6$), the format nginx reads through the system crypt()."""
    try:
        done = subprocess.run(["openssl", "passwd", "-6", "-stdin"], input=password + "\n",
                              capture_output=True, text=True, check=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        raise AuthError(f"openssl could not hash the password: {error}") from None
    result = done.stdout.strip()
    if not result.startswith("$6$"):
        raise AuthError("openssl did not return a SHA-512 password hash.")
    return result


def list_edl_users(conn: sqlite3.Connection) -> list[dict[str, str]]:
    return [{"name": r["name"], "created_at": r["created_at"], "created_by": r["created_by"]}
            for r in conn.execute("SELECT * FROM edl_users ORDER BY name")]


def add_edl_user(conn: sqlite3.Connection, name: str, actor: str) -> str:
    name = check_name(name)
    password = new_password()
    hashed = hash_password(password)
    with db.transaction(conn):
        if conn.execute("SELECT 1 FROM edl_users WHERE name = ?", (name,)).fetchone():
            raise AuthError(f"An EDL login named '{name}' exists already. Use 'edl-user rotate {name}'.")
        conn.execute("INSERT INTO edl_users(name, password_hash, created_at, created_by) VALUES (?,?,?,?)",
                     (name, hashed, db.to_iso(db.utcnow()), actor))
        db.audit(conn, "edl_user_add", "edl_user", name, "ok", actor=actor, source="cli")
    return password


def rotate_edl_user(conn: sqlite3.Connection, name: str, actor: str) -> str:
    name = check_name(name)
    password = new_password()
    hashed = hash_password(password)
    with db.transaction(conn):
        changed = conn.execute("UPDATE edl_users SET password_hash = ?, created_at = ?, created_by = ? WHERE name = ?",
                               (hashed, db.to_iso(db.utcnow()), actor, name)).rowcount
        if not changed:
            raise AuthError(f"There is no EDL login named '{name}'. See: furnace edl-user list")
        db.audit(conn, "edl_user_rotate", "edl_user", name, "ok", actor=actor, source="cli")
    return password


def remove_edl_user(conn: sqlite3.Connection, name: str, actor: str) -> None:
    name = check_name(name)
    with db.transaction(conn):
        if not conn.execute("DELETE FROM edl_users WHERE name = ?", (name,)).rowcount:
            raise AuthError(f"There is no EDL login named '{name}'. See: furnace edl-user list")
        db.audit(conn, "edl_user_remove", "edl_user", name, "ok", actor=actor, source="cli")


# ---------------------------------------------------------------------- lockout

LOCKOUT_SCHEMA = """
CREATE TABLE IF NOT EXISTS failures (
    ip           TEXT PRIMARY KEY,
    count        INTEGER NOT NULL,
    first_at     REAL NOT NULL,
    locked_until REAL NOT NULL DEFAULT 0
);
"""


def _lockout_db(settings: Settings) -> sqlite3.Connection:
    conn = sqlite3.connect(str(settings.lockout_db_path), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(LOCKOUT_SCHEMA)
    return conn


def locked_for(settings: Settings, ip: str, now: float | None = None) -> int:
    """Seconds this IP stays locked, or 0."""
    now = time.time() if now is None else now
    conn = _lockout_db(settings)
    try:
        row = conn.execute("SELECT locked_until FROM failures WHERE ip = ?", (ip,)).fetchone()
    finally:
        conn.close()
    return max(0, int(row["locked_until"] - now) + 1) if row and row["locked_until"] > now else 0


def record_failure(settings: Settings, ip: str, now: float | None = None) -> bool:
    """Count one wrong token from this IP. True when the IP is locked now."""
    now = time.time() if now is None else now
    window = settings.lockout_minutes * 60
    conn = _lockout_db(settings)
    try:
        with db.transaction(conn):
            row = conn.execute("SELECT * FROM failures WHERE ip = ?", (ip,)).fetchone()
            if row is None or row["first_at"] < now - window:
                count, first = 1, now
            else:
                count, first = row["count"] + 1, row["first_at"]
            locked_until = now + window if count >= settings.lockout_failures else 0
            conn.execute("INSERT OR REPLACE INTO failures(ip, count, first_at, locked_until) VALUES (?,?,?,?)",
                         (ip, count, first, locked_until))
            conn.execute("DELETE FROM failures WHERE first_at < ? AND locked_until < ?", (now - window, now))
    finally:
        conn.close()
    return locked_until > 0


def list_lockouts(settings: Settings, now: float | None = None) -> list[dict[str, object]]:
    now = time.time() if now is None else now
    conn = _lockout_db(settings)
    try:
        rows = conn.execute("SELECT * FROM failures WHERE locked_until > ? ORDER BY ip", (now,)).fetchall()
    finally:
        conn.close()
    return [{"ip": r["ip"], "wrong_tokens": r["count"], "seconds_left": int(r["locked_until"] - now) + 1}
            for r in rows]


def unlock(settings: Settings, ip: str | None) -> int:
    conn = _lockout_db(settings)
    try:
        if ip is None:
            return conn.execute("DELETE FROM failures").rowcount
        return conn.execute("DELETE FROM failures WHERE ip = ?", (ip,)).rowcount
    finally:
        conn.close()

