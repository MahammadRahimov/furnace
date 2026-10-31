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

SCHEMA_VERSION = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Active IP blocklist. aging_from is the date the cleanup uses.
-- It equals added_at, except for undated imports, where it is spread out.
-- permanent = 1: the cleanup never removes the IP. Only an unblock does.
CREATE TABLE IF NOT EXISTS blocked_ips (
    ip          TEXT PRIMARY KEY,
    version     INTEGER NOT NULL,
    added_at    TEXT NOT NULL,
    aging_from  TEXT NOT NULL,
    added_by    TEXT NOT NULL DEFAULT '',
    reason      TEXT NOT NULL DEFAULT '',
    incident_id TEXT NOT NULL DEFAULT '',
    permanent   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_blocked_ips_aging ON blocked_ips(aging_from);

-- The archive: a record of every different IP and domain ever blocked. One row each,
-- written when it is first blocked, never removed. added_by and reason are
-- the details of the first block. Blocked now = also in blocked_ips / blocked_domains.
CREATE TABLE IF NOT EXISTS ip_records (
    ip               TEXT PRIMARY KEY,
    version          INTEGER NOT NULL,
    first_blocked_at TEXT NOT NULL,
    last_blocked_at  TEXT NOT NULL,
    times_blocked    INTEGER NOT NULL DEFAULT 1,
    added_by         TEXT NOT NULL DEFAULT '',
    reason           TEXT NOT NULL DEFAULT '',
    incident_id      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS domain_records (
    domain           TEXT PRIMARY KEY,
    first_blocked_at TEXT NOT NULL,
    last_blocked_at  TEXT NOT NULL,
    times_blocked    INTEGER NOT NULL DEFAULT 1,
    added_by         TEXT NOT NULL DEFAULT '',
    reason           TEXT NOT NULL DEFAULT '',
    incident_id      TEXT NOT NULL DEFAULT ''
);

-- The archive of version 0.6 and older (expired IPs only). Version 0.7 moves its rows
-- to ip_records and leaves it empty. It stays, so a follower that still runs 0.6 accepts
-- the snapshots of a 0.7 leader during an upgrade.
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

-- API tokens. Only the SHA-256 hash of a token is stored.
-- permissions: comma list of blocklist:read, blocklist:write, whitelist:read, whitelist:write.
-- scope: the same as in version 0.5 (block, whitelist, all or custom), for an older follower.
-- expires_at: '' = never. expires_days: the length, used again by "token rotate".
CREATE TABLE IF NOT EXISTS api_tokens (
    name         TEXT PRIMARY KEY,
    token_hash   TEXT NOT NULL UNIQUE,
    scope        TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    created_by   TEXT NOT NULL DEFAULT '',
    permissions  TEXT NOT NULL DEFAULT '',
    expires_days INTEGER NOT NULL DEFAULT 0,
    expires_at   TEXT NOT NULL DEFAULT '',
    last_used_at TEXT NOT NULL DEFAULT ''
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
        migrate(conn)
    finally:
        conn.close()


# Columns added after the first release of a table: (table, column, definition).
ADDED_COLUMNS = (
    ("api_tokens", "permissions", "TEXT NOT NULL DEFAULT ''"),
    ("api_tokens", "expires_days", "INTEGER NOT NULL DEFAULT 0"),
    ("api_tokens", "expires_at", "TEXT NOT NULL DEFAULT ''"),
    ("api_tokens", "last_used_at", "TEXT NOT NULL DEFAULT ''"),
    ("blocked_ips", "permanent", "INTEGER NOT NULL DEFAULT 0"),
)
SCOPE_PERMISSIONS = {  # version 0.5 scopes -> permissions
    "block": "blocklist:read,blocklist:write",
    "whitelist": "whitelist:read,whitelist:write",
    "all": "blocklist:read,blocklist:write,whitelist:read,whitelist:write",
}


def migrate(conn: sqlite3.Connection) -> None:
    """Bring a database from an older version (or a snapshot from an older leader) up to date.

    Old tokens keep their rights and never expire. Old IPs stay temporary with their dates.
    """
    for table, column, definition in ADDED_COLUMNS:
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    for scope, permissions in SCOPE_PERMISSIONS.items():
        conn.execute("UPDATE api_tokens SET permissions = ? WHERE permissions = '' AND scope = ?",
                     (permissions, scope))
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None or int(row[0]) < 4:
        fill_records(conn)
    if row is None or int(row[0]) < 5:
        clear_incident_ids(conn)
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))


# Version 0.8 removed incident_id (user decision 2026-11-01). The columns stay, so older followers and
# backups still fit, but they are always empty: the upgrade deletes every stored value once.
INCIDENT_TABLES = ("blocked_ips", "blocked_domains", "ip_records", "domain_records", "archived_ips", "audit_log")


def clear_incident_ids(conn: sqlite3.Connection) -> None:
    for table in INCIDENT_TABLES:
        conn.execute(f"UPDATE {table} SET incident_id = '' WHERE incident_id <> ''")


def fill_records(conn: sqlite3.Connection) -> None:
    """Version 0.7: every blocked IP and domain and every old archived IP gets a record row.
    Rows that exist already are kept. The old archive table is emptied."""
    conn.execute("INSERT OR IGNORE INTO ip_records(ip, version, first_blocked_at, last_blocked_at, added_by,"
                 " reason, incident_id) SELECT ip, version, added_at, added_at, added_by, reason, incident_id"
                 " FROM blocked_ips")
    conn.execute("INSERT OR IGNORE INTO ip_records(ip, version, first_blocked_at, last_blocked_at, added_by,"
                 " reason, incident_id) SELECT ip, version, added_at, added_at, added_by, reason, incident_id"
                 " FROM archived_ips")
    conn.execute("DELETE FROM archived_ips")
    conn.execute("INSERT OR IGNORE INTO domain_records(domain, first_blocked_at, last_blocked_at, added_by,"
                 " reason, incident_id) SELECT domain, added_at, added_at, added_by, reason, incident_id"
                 " FROM blocked_domains")


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
