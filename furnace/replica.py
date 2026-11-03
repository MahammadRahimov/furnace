"""Apply a snapshot from the leader on the follower.

The leader pushes a full database snapshot every 20 seconds. The follower
checks it, copies it into its own database with the SQLite backup
function (one step, safe with WAL), and rebuilds its EDL files.
A leader never applies a snapshot, so a late push cannot overwrite it.

The snapshot comes over HTTPS (send_snapshot on the leader, receive_snapshot in the
follower's API) with a secret both servers share. On servers that secret is derived from
the TLS key they already share (derive_secret). For one version the servers can still fall
back to SSH, so an upgrade works while the follower is still old.
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import os
import sqlite3
import ssl
import tempfile
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from typing import BinaryIO, Callable

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


def derive_secret(key_file: Path) -> str:
    """The replication secret of two servers: both have the same TLS key (from the leader's bundle),
    so both get the same secret without copying anything new. A new key gives a new secret."""
    return hmac.new(Path(key_file).read_bytes(), b"furnace replication v1", hashlib.sha256).hexdigest()


# Python 3.9 does not name this OpenSSL flag yet; the value is the same in OpenSSL 1.1.1 and 3.
VERIFY_X509_PARTIAL_CHAIN = getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0x80000)


def replica_context(cafile: Path) -> ssl.SSLContext:
    """TLS for the replication: trust only the shared Furnace certificate, and trust it directly
    (partial chain). Then a certificate made before 0.8 on RHEL 8 ("Basic Constraints" twice, so
    OpenSSL does not accept it as its own CA) still works. The host name check stays on."""
    context = ssl.create_default_context(cafile=str(cafile))
    context.verify_flags |= VERIFY_X509_PARTIAL_CHAIN
    return context


def send_snapshot(settings: Settings, peer_ip: str, cafile: Path, secret_file: Path, out: Path,
                  opener: Callable | None = None) -> str:
    """Leader: send one gzip snapshot to the other server. Returns a short result; raises ReplicaError."""
    from . import backup

    if not settings.is_leader():
        return "not the leader"
    secret = read_secret(secret_file)
    out.parent.mkdir(parents=True, exist_ok=True)
    backup.snapshot(settings, out)
    body = gzip.compress(out.read_bytes(), compresslevel=6)
    request = urllib.request.Request(
        f"https://{peer_ip}/replica/snapshot", data=body, method="POST",
        headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/octet-stream"})
    if opener is None:
        context = replica_context(cafile)
        opener = lambda req: urllib.request.urlopen(req, timeout=50, context=context)  # noqa: E731
    try:
        with opener(request) as reply:
            answer = json.loads(reply.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read() or b"{}").get("message", "")
        except ValueError:
            message = ""
        if exc.code == 404:
            message = "it does not take snapshots over HTTPS (an older Furnace)"
        raise ReplicaError(f"the other server answered {exc.code}: {message}".strip(": ")) from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ReplicaError(f"cannot reach the other server at {peer_ip}: {reason}") from None
    applied = answer.get("applied", {})
    urls = f", {applied['urls']} URLs" if "urls" in applied else ""  # an older follower does not count URLs
    return (f"sent ({len(body)} bytes, {applied.get('ips', '?')} IPs, "
            f"{applied.get('domains', '?')} domains{urls})")


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
