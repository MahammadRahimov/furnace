"""Nightly cleanup: move old IPs from the active list to the archive.

Safety stop: if one run would remove more than FURNACE_CLEANUP_MAX_PERCENT
(default 20) percent of a list with at least FURNACE_CLEANUP_MIN_SIZE
(default 100) IPs, nothing is moved and the job exits with an error,
unless --force is used.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import db, edl
from .config import Settings
from .locking import FileLock
from .validation import ip_sort_key

log = logging.getLogger("furnace")

EXIT_OK = 0
EXIT_SAFETY_STOP = 3


@dataclass
class CleanupResult:
    exit_code: int
    message: str
    moved: list[str] = field(default_factory=list)
    total_before: int = 0


def run_cleanup(settings: Settings, now: datetime | None = None, dry_run: bool = False,
                force: bool = False) -> CleanupResult:
    if not settings.is_leader():
        return CleanupResult(EXIT_OK, "This server is the standby. The cleanup runs only on the leader.")
    now = now or db.utcnow()
    cutoff = db.to_iso(now - timedelta(days=settings.archive_after_days))
    with FileLock(settings.lock_path, settings.lock_timeout):
        conn = db.connect(settings.db_path)
        try:
            total = conn.execute("SELECT COUNT(*) FROM blocked_ips").fetchone()[0]
            old = sorted((row[0] for row in conn.execute(
                "SELECT ip FROM blocked_ips WHERE aging_from < ?", (cutoff,))), key=ip_sort_key)
            percent = (100.0 * len(old) / total) if total else 0.0

            if (total >= settings.cleanup_min_size and percent > settings.cleanup_max_percent
                    and not force):
                message = (f"Safety stop: the cleanup would remove {len(old)} of {total} IPs "
                           f"({percent:.1f} percent). The limit is {settings.cleanup_max_percent} percent. "
                           "Nothing was changed. Check the dates, then run with --force if this is correct.")
                if not dry_run:
                    with db.transaction(conn):
                        db.audit(conn, "cleanup", "ip", "", "stopped", "safety_stop", "cleanup", "",
                                 "cleanup", message, now)
                return CleanupResult(EXIT_SAFETY_STOP, message, old, total)

            if dry_run:
                return CleanupResult(EXIT_OK, f"Dry run: {len(old)} of {total} IPs would move to the archive "
                                              f"(older than {settings.archive_after_days} days).", old, total)

            stamp = db.to_iso(now)
            with db.transaction(conn):
                conn.executemany(
                    "INSERT OR REPLACE INTO archived_ips(ip, version, added_at, archived_at, added_by, reason,"
                    " incident_id) SELECT ip, version, added_at, ?, added_by, reason, incident_id"
                    " FROM blocked_ips WHERE ip=?", [(stamp, ip) for ip in old])
                conn.executemany("DELETE FROM blocked_ips WHERE ip=?", [(ip,) for ip in old])
                for ip in old:
                    db.audit(conn, "archive", "ip", ip, "archived", "", "cleanup", "", "cleanup", "", now)
                db.audit(conn, "cleanup", "ip", "", "done", "", "cleanup", "", "cleanup",
                         f"Moved {len(old)} of {total} IPs to the archive."
                         + (" Forced." if force else ""), now)
            edl.write_blocklist(conn, settings)
            edl.write_archive_csv(conn, settings)
        finally:
            conn.close()
    return CleanupResult(EXIT_OK, f"Moved {len(old)} of {total} IPs to the archive.", old, total)
