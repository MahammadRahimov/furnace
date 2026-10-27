"""Writes the EDL files and the archive CSV from the database.

Each file is written to a temporary file in the same folder, synced to
disk, then renamed over the old one. A rename is atomic, so the firewall
never sees half a file.
"""

from __future__ import annotations

import csv
import io
import os
import sqlite3
import tempfile
from pathlib import Path

from .config import Settings
from .validation import ip_sort_key


def atomic_write(path: Path, content: str, mode: int = 0o644) -> None:
    path = Path(path)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _lines(values: list[str]) -> str:
    return "".join(f"{v}\n" for v in values)


def write_blocklist(conn: sqlite3.Connection, settings: Settings) -> int:
    ips = [row[0] for row in conn.execute("SELECT ip FROM blocked_ips")]
    ips.sort(key=ip_sort_key)
    atomic_write(settings.blocklist_path, _lines(ips))
    return len(ips)


def write_domains(conn: sqlite3.Connection, settings: Settings) -> int:
    domains = [row[0] for row in conn.execute("SELECT domain FROM blocked_domains ORDER BY domain")]
    atomic_write(settings.domains_path, _lines(domains))
    return len(domains)


ARCHIVE_COLUMNS = ["ip", "added_at", "archived_at", "added_by", "reason", "incident_id"]


def write_archive_csv(conn: sqlite3.Connection, settings: Settings) -> int:
    """Storage only. Not an EDL. nginx never serves this folder."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(ARCHIVE_COLUMNS)
    count = 0
    for row in conn.execute(f"SELECT {', '.join(ARCHIVE_COLUMNS)} FROM archived_ips "
                            "ORDER BY archived_at, ip"):
        writer.writerow(list(row))
        count += 1
    atomic_write(settings.archive_csv_path, buffer.getvalue(), mode=0o640)
    return count


def write_edl_users(conn: sqlite3.Connection, settings: Settings) -> int:
    """The nginx users file for the EDL login (name:hash lines). Never served."""
    try:
        rows = conn.execute("SELECT name, password_hash FROM edl_users ORDER BY name").fetchall()
    except sqlite3.OperationalError:  # a snapshot from an older version without the table
        rows = []
    atomic_write(settings.edl_users_path, "".join(f"{r[0]}:{r[1]}\n" for r in rows), mode=0o640)
    return len(rows)


def rebuild_all(conn: sqlite3.Connection, settings: Settings) -> dict[str, int]:
    return {
        "ips": write_blocklist(conn, settings),
        "domains": write_domains(conn, settings),
        "archived": write_archive_csv(conn, settings),
        "edl_users": write_edl_users(conn, settings),
    }


def ensure_files(settings: Settings) -> None:
    """A fresh server must never return an error page: create empty files.
    An empty users file means that nobody may download the EDL yet."""
    for path in (settings.blocklist_path, settings.domains_path):
        if not path.exists():
            atomic_write(path, "")
    if not settings.edl_users_path.exists():
        atomic_write(settings.edl_users_path, "", mode=0o640)
