"""Apply a snapshot from the leader on the follower.

The leader pushes a full database snapshot every minute. The follower
checks it, copies it into its own database with the SQLite backup
function (one step, safe with WAL), and rebuilds its EDL files.
A leader never applies a snapshot, so a late push cannot overwrite it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import db, edl
from .config import Settings
from .locking import FileLock

REQUIRED_TABLES = {"blocked_ips", "archived_ips", "blocked_domains", "whitelist_ips",
                   "whitelist_domains", "audit_log", "meta"}


class ReplicaError(Exception):
    pass


def apply_snapshot(settings: Settings, source: Path) -> dict[str, int]:
    if settings.is_leader():
        raise ReplicaError("This server is the leader. It does not take snapshots from the other server.")
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ReplicaError("The snapshot is damaged. It was not applied.")
        tables = {row[0] for row in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not REQUIRED_TABLES <= tables:
            raise ReplicaError("The snapshot is not a Furnace database. It was not applied.")
        with FileLock(settings.lock_path, settings.lock_timeout):
            live = db.connect(settings.db_path)
            try:
                src.backup(live)
                live.execute("PRAGMA journal_mode=WAL")
                return edl.rebuild_all(live, settings)
            finally:
                live.close()
    finally:
        src.close()
