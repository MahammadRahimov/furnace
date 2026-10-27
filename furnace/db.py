"""SQLite storage. The database is the master record.

WAL mode (Write-Ahead Logging) with synchronous FULL, so a committed
change survives a power cut. Every change runs in one transaction.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Active IP blocklist. aging_from is the date the cleanup uses.
-- It equals added_at, except for undated imports, where it is spread out.
CREATE TABLE IF NOT EXISTS blocked_ips (
    ip          TEXT PRIMARY KEY,
    version     INTEGER NOT NULL,
    added_at    TEXT NOT NULL,
    aging_from  TEXT NOT NULL,
    added_by    TEXT NOT NULL DEFAULT '',
    reason      TEXT NOT NULL DEFAULT '',
    incident_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_blocked_ips_aging ON blocked_ips(aging_from);

CREATE TABLE IF NOT EXISTS archived_ips (
    ip          TEXT PRIMARY KEY,
    version     INTEGER NOT NULL,
    added_at    TEXT NOT NULL,
    archived_at TEXT NOT NULL,
    added_by    TEXT NOT NULL DEFAULT '',
    reason      TEXT NOT NULL DEFAULT '',
    incident_id TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS blocked_domains (
    domain      TEXT PRIMARY KEY,
    added_at    TEXT NOT NULL,
    added_by    TEXT NOT NULL DEFAULT '',
    reason      TEXT NOT NULL DEFAULT '',
    incident_id TEXT NOT NULL DEFAULT ''
);

-- start_hex / end_hex: the range as fixed-width hex, so text order = number order.
CREATE TABLE IF NOT EXISTS whitelist_ips (
    cidr      TEXT PRIMARY KEY,
    version   INTEGER NOT NULL,
    start_hex TEXT NOT NULL,
    end_hex   TEXT NOT NULL,
    added_at  TEXT NOT NULL,
    added_by  TEXT NOT NULL DEFAULT '',
    reason    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_whitelist_ips_range ON whitelist_ips(version, start_hex, end_hex);

CREATE TABLE IF NOT EXISTS whitelist_domains (
    domain   TEXT PRIMARY KEY,
    added_at TEXT NOT NULL,
    added_by TEXT NOT NULL DEFAULT '',
    reason   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    action      TEXT NOT NULL,
    object_type TEXT NOT NULL,
    value       TEXT NOT NULL DEFAULT '',
    result      TEXT NOT NULL,
    reason_code TEXT NOT NULL DEFAULT '',
    actor       TEXT NOT NULL DEFAULT '',
    incident_id TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',
    details     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);

-- API tokens. Only the SHA-256 hash of a token is stored. scope: block, whitelist or all.
CREATE TABLE IF NOT EXISTS api_tokens (
    name       TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    scope      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT ''
);

-- Logins for the EDL download (nginx basic auth). password_hash is SHA-512 crypt.
CREATE TABLE IF NOT EXISTS edl_users (
    name          TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    created_by    TEXT NOT NULL DEFAULT ''
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(path: Path) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                     (str(SCHEMA_VERSION),))
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One change = one transaction. Rolls back on any error."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def audit(conn: sqlite3.Connection, action: str, object_type: str, value: str, result: str,
          reason_code: str = "", actor: str = "", incident_id: str = "", source: str = "",
          details: str = "", now: datetime | None = None) -> None:
    conn.execute(
        "INSERT INTO audit_log(ts, action, object_type, value, result, reason_code, actor,"
        " incident_id, source, details) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (to_iso(now or utcnow()), action, object_type, value[:300], result, reason_code,
         actor[:200], incident_id[:200], source[:100], details[:2000]),
    )


def backup_to(conn: sqlite3.Connection, target: Path) -> None:
    """Copy the live database with the SQLite backup function (safe while in use)."""
    dest = sqlite3.connect(str(target))
    try:
        conn.backup(dest)
    finally:
        dest.close()
