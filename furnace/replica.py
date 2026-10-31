"""Apply a snapshot from the leader on the follower.

The leader pushes a full database snapshot every minute. The follower
checks it, copies it into its own database with the SQLite backup
function (one step, safe with WAL), and rebuilds its EDL files.
A leader never applies a snapshot, so a late push cannot overwrite it.

On servers the snapshot comes over SSH. In Docker it comes over HTTPS
(receive_snapshot, called by the API) with a secret both servers share.
"""

from __future__ import annotations

import gzip
import hmac
import os
import sqlite3
import tempfile
import zlib
from pathlib import Path
from typing import BinaryIO

from . import db, edl
from .config import Settings
from .locking import FileLock

REQUIRED_TABLES = {"blocked_ips", "archived_ips", "blocked_domains", "whitelist_ips",
                   "whitelist_domains", "audit_log", "meta"}


MAX_SNAPSHOT_BYTES = 2 * 1024 ** 3  # 2 GB, so a broken sender cannot fill the disk
SNAPSHOT_MAGIC = b"SQLite format 3\x00"


class ReplicaError(Exception):
    pass


def read_secret(path: Path) -> str:
    try:
        secret = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ReplicaError(f"Cannot read the replication secret {path}: {exc.strerror}.") from None
    if len(secret) < 32:
        raise ReplicaError(f"The replication secret in {path} is too short.")
    return secret


def secret_matches(path: Path, given: str) -> bool:
    try:
        expected = read_secret(path)
    except ReplicaError:
        return False
    return bool(given) and hmac.compare_digest(given.encode(), expected.encode())


def receive_snapshot(settings: Settings, stream: BinaryIO, limit: int = MAX_SNAPSHOT_BYTES) -> dict[str, int]:
    """Unpack a gzip snapshot from the leader into the incoming folder, then apply it."""
    folder = settings.incoming_dir
    folder.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=folder, prefix=".snapshot.", suffix=".tmp")
    tmp = Path(name)
    try:
        size = 0
        with os.fdopen(fd, "wb") as out:
            try:
                with gzip.GzipFile(fileobj=stream, mode="rb") as unpacked:
                    while True:
                        chunk = unpacked.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > limit:
                            raise ReplicaError("The snapshot is too large. It was dropped.")
                        out.write(chunk)
            except (OSError, EOFError, zlib.error):
                raise ReplicaError("The snapshot is not valid gzip data. It was dropped.") from None
        with open(tmp, "rb") as check:
            if check.read(len(SNAPSHOT_MAGIC)) != SNAPSHOT_MAGIC:
                raise ReplicaError("The received data is not a SQLite database. It was dropped.")
        target = folder / "snapshot.db"
        os.replace(tmp, target)
        try:
            return apply_snapshot(settings, target)
        finally:
            target.unlink(missing_ok=True)
    finally:
        tmp.unlink(missing_ok=True)


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
                live.executescript(db.SCHEMA)  # a leader with an older version has fewer tables
                db.migrate(live)  # ... and fewer columns
                return edl.rebuild_all(live, settings)
            finally:
                live.close()
    finally:
        src.close()
