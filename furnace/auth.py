"""API tokens, the wrong-token lockout and the EDL logins.

API tokens and EDL logins live in the database, so the replication copies
them to the other server. Only a hash is stored. A token or a password is
shown once, when it is made. Lost? Make a new one with "rotate".

API tokens: SHA-256 of a long random token. A token has a name (who uses it,
for example "soar" or "ui"), permissions and an expiry. There are four
permissions, a grid of two lists and two actions:

                 read   write
    blocklist     x      x       look up / block and unblock IPs and domains
    whitelist     x      x       list and look up / add and remove entries

Write always includes read. Each token has its own expiry in days (0 = never),
chosen when the token is made.

EDL logins: nginx basic auth. The password hash is SHA-512 crypt ($6$),
made by openssl. nginx reads the users file on every request, so a change
works without a reload.

Lockout: a caller IP that sends too many wrong tokens is locked for a while.
It is kept in its own small database on each server and is not replicated.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import re
import secrets
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable

from . import db
from .config import Settings

log = logging.getLogger("furnace")

LISTS = ("blocklist", "whitelist")
PERMISSIONS = ("blocklist:read", "blocklist:write", "whitelist:read", "whitelist:write")
# Short names. block, whitelist and all are the scopes of version 0.5 and older.
ALIASES = {
    "all": PERMISSIONS,
    "block": ("blocklist:read", "blocklist:write"),
    "blocklist": ("blocklist:read", "blocklist:write"),
    "whitelist": ("whitelist:read", "whitelist:write"),
}
MAX_EXPIRES_DAYS = 3650
LAST_USED_EVERY = 3600  # seconds: "last used" is written at most once an hour per token
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
TOKEN_PREFIX = "fur_"


class AuthError(Exception):
    """A token or login command cannot be done. The message says why."""


@dataclass
class Client:
    name: str
    permissions: tuple[str, ...]
    expires_at: str = ""
    token_hash: str = field(default="", repr=False)

    def may(self, list_name: str, action: str) -> bool:
        return f"{list_name}:{action}" in self.permissions

    def expired(self, now: datetime | None = None) -> bool:
        return bool(self.expires_at) and db.from_iso(self.expires_at) <= (now or db.utcnow())


def check_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise AuthError("A name has 1 to 40 characters: letters, numbers, dot, dash or underscore. "
                        "It starts with a letter or a number.")
    return name


def parse_permissions(value: str | Iterable[str]) -> tuple[str, ...]:
    """'blocklist:write,whitelist:read', 'all', 'block' ... -> the permissions in a fixed order.

    Write always brings read with it.
    """
    items = value.split(",") if isinstance(value, str) else list(value)
    found: set[str] = set()
    for item in items:
        item = str(item).strip().lower()
        if not item:
            continue
        if item in ALIASES:
            found.update(ALIASES[item])
        elif item in PERMISSIONS:
            found.add(item)
        else:
            raise AuthError(f"Unknown permission '{item}'. Use {', '.join(PERMISSIONS)} or all.")
    for list_name in LISTS:
        if f"{list_name}:write" in found:
            found.add(f"{list_name}:read")
    if not found:
        raise AuthError("A token needs at least one permission.")
    return tuple(p for p in PERMISSIONS if p in found)


def describe_permissions(permissions: Iterable[str]) -> str:
    """('blocklist:read', 'blocklist:write', 'whitelist:read') -> 'blocklist read+write, whitelist read'."""
    perms = set(permissions)
    parts = []
    for list_name in LISTS:
        actions = [a for a in ("read", "write") if f"{list_name}:{a}" in perms]
        parts.append(f"{list_name} {'+'.join(actions) if actions else '-'}")
    return ", ".join(parts)


def legacy_scope(permissions: Iterable[str]) -> str:
    """The scope column, for a follower that still runs version 0.5 during an upgrade."""
    perms = set(permissions)
    if perms == set(PERMISSIONS):
        return "all"
    if perms == set(ALIASES["block"]):
        return "block"
    if perms == set(ALIASES["whitelist"]):
        return "whitelist"
    return "custom"  # version 0.5 refuses it, which is the safe side


def check_expires_days(days: object) -> int:
    try:
        number = int(str(days).strip())
    except (TypeError, ValueError):
        number = -1
    if not 0 <= number <= MAX_EXPIRES_DAYS:
        raise AuthError(f"The expiry is a number of days from 0 to {MAX_EXPIRES_DAYS} (0 = never).")
    return number


def expiry_from(days: int, now: datetime) -> str:
    return db.to_iso(now + timedelta(days=days)) if days else ""


def describe_expiry(expires_at: str, now: datetime | None = None) -> str:
    if not expires_at:
        return "never"
    left = db.from_iso(expires_at) - (now or db.utcnow())
    day = expires_at[:10]
    if left.total_seconds() <= 0:
        return f"EXPIRED {day}"
    days = math.ceil(left.total_seconds() / 86400)
    return f"{day} (in {days} day{'' if days == 1 else 's'})"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def new_password() -> str:
    return secrets.token_urlsafe(24)


# ---------------------------------------------------------------------- API tokens

def _permissions_of(row: sqlite3.Row) -> tuple[str, ...]:
    try:
        return parse_permissions(row["permissions"] or row["scope"])
    except AuthError:  # a scope this version does not know: no permissions
        return ()


def find_client(settings: Settings, token: str) -> Client | None:
    """The client that owns this token, or None. The caller checks the expiry and the permissions."""
    if not token:
        return None
    selftest = settings.selftest_token()
    if selftest and hmac.compare_digest(token.encode("utf-8"), selftest.encode("utf-8")):
        return Client("setup-selftest", PERMISSIONS)
    conn = db.connect(settings.db_path)
    try:
        row = conn.execute("SELECT * FROM api_tokens WHERE token_hash = ?", (hash_token(token),)).fetchone()
    except sqlite3.OperationalError:  # a snapshot from an older version without the table
        return None
    finally:
        conn.close()
    if row is None:
        return None
    return Client(row["name"], _permissions_of(row), row["expires_at"], row["token_hash"])


def mark_used(settings: Settings, client: Client, now: datetime | None = None) -> None:
    """Remember when a token was last used. Leader only, at most once an hour. Never fails a call."""
    if not client.token_hash or not settings.is_leader():
        return
    now = now or db.utcnow()
    try:
        conn = db.connect(settings.db_path)
        try:
            conn.execute("UPDATE api_tokens SET last_used_at = ? WHERE token_hash = ? AND last_used_at < ?",
                         (db.to_iso(now), client.token_hash, db.to_iso(now - timedelta(seconds=LAST_USED_EVERY))))
        finally:
            conn.close()
    except sqlite3.Error as error:
        log.warning("Could not save when the token '%s' was last used: %s", client.name, error)


def list_tokens(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict[str, object]]:
    now = now or db.utcnow()
    return [{"name": r["name"], "permissions": _permissions_of(r), "expires_days": r["expires_days"],
             "expires_at": r["expires_at"], "expires": describe_expiry(r["expires_at"], now),
             "expired": bool(r["expires_at"]) and db.from_iso(r["expires_at"]) <= now,
             "last_used_at": r["last_used_at"], "created_at": r["created_at"],
             "created_by": r["created_by"], "id": r["token_hash"][:8]}
            for r in conn.execute("SELECT * FROM api_tokens ORDER BY name")]


def expiring_tokens(conn: sqlite3.Connection, within_days: int = 14,
                    now: datetime | None = None) -> list[dict[str, object]]:
    """Tokens that expire within the next days, and tokens that have expired."""
    now = now or db.utcnow()
    limit = db.to_iso(now + timedelta(days=within_days))
    return [t for t in list_tokens(conn, now) if t["expires_at"] and str(t["expires_at"]) <= limit]


def get_token(conn: sqlite3.Connection, name: str) -> dict[str, object]:
    name = check_name(name)
    for token in list_tokens(conn):
        if token["name"] == name:
            return token
    raise AuthError(f"There is no token named '{name}'. See: furnace token list")


def add_token(conn: sqlite3.Connection, name: str, permissions: str | Iterable[str], actor: str,
              token: str | None = None, expires_days: object = 0) -> str:
    name = check_name(name)
    perms = parse_permissions(permissions)
    days = check_expires_days(expires_days)
    token = token or new_token()
    now = db.utcnow()
    with db.transaction(conn):
        if conn.execute("SELECT 1 FROM api_tokens WHERE name = ?", (name,)).fetchone():
            raise AuthError(f"A token named '{name}' exists already. Use 'token rotate {name}' for a new one.")
        conn.execute("INSERT INTO api_tokens(name, token_hash, scope, permissions, created_at, created_by,"
                     " expires_days, expires_at) VALUES (?,?,?,?,?,?,?,?)",
                     (name, hash_token(token), legacy_scope(perms), ",".join(perms), db.to_iso(now), actor,
                      days, expiry_from(days, now)))
        db.audit(conn, "token_add", "api_token", name, "ok", actor=actor, source="cli",
                 details=f"permissions={','.join(perms)} expires_days={days}")
    return token


def rotate_token(conn: sqlite3.Connection, name: str, actor: str, expires_days: object = None) -> str:
    """A new value. The expiry starts again: the same number of days, or the new ones."""
    name = check_name(name)
    token = new_token()
    now = db.utcnow()
    with db.transaction(conn):
        row = conn.execute("SELECT expires_days FROM api_tokens WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise AuthError(f"There is no token named '{name}'. See: furnace token list")
        days = row["expires_days"] if expires_days is None else check_expires_days(expires_days)
        conn.execute("UPDATE api_tokens SET token_hash = ?, created_at = ?, created_by = ?, expires_days = ?,"
                     " expires_at = ?, last_used_at = '' WHERE name = ?",
                     (hash_token(token), db.to_iso(now), actor, days, expiry_from(days, now), name))
        db.audit(conn, "token_rotate", "api_token", name, "ok", actor=actor, source="cli",
                 details=f"expires_days={days}")
    return token


def edit_token(conn: sqlite3.Connection, name: str, actor: str, permissions: str | Iterable[str] | None = None,
               expires_days: object = None) -> None:
    """Change the permissions and/or the expiry (counted from now). The token value stays."""
    name = check_name(name)
    if permissions is None and expires_days is None:
        raise AuthError("Nothing to change. Give --allow and/or --expires-days.")
    now = db.utcnow()
    with db.transaction(conn):
        row = conn.execute("SELECT * FROM api_tokens WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise AuthError(f"There is no token named '{name}'. See: furnace token list")
        perms = _permissions_of(row) if permissions is None else parse_permissions(permissions)
        if expires_days is None:
            days, expires_at = row["expires_days"], row["expires_at"]
        else:
            days = check_expires_days(expires_days)
            expires_at = expiry_from(days, now)
        conn.execute("UPDATE api_tokens SET permissions = ?, scope = ?, expires_days = ?, expires_at = ?"
                     " WHERE name = ?", (",".join(perms), legacy_scope(perms), days, expires_at, name))
        db.audit(conn, "token_edit", "api_token", name, "ok", actor=actor, source="cli",
                 details=f"permissions={','.join(perms)} expires_days={days}")


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

