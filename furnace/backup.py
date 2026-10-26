"""Database backups with the SQLite backup function. Keeps the newest N copies."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .config import Settings


def make_backup(settings: Settings, label: str = "backup", keep: int | None = None) -> Path:
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    target = settings.backup_dir / f"furnace-{stamp}-{label}.db"
    tmp = target.with_suffix(".db.part")
    conn = db.connect(settings.db_path)
    try:
        db.backup_to(conn, tmp)
    finally:
        conn.close()
    os.chmod(tmp, 0o640)
    os.replace(tmp, target)
    prune(settings, keep if keep is not None else settings.backup_keep)
    return target


def prune(settings: Settings, keep: int) -> list[Path]:
    copies = sorted(settings.backup_dir.glob("furnace-*.db"))
    old = copies[:-keep] if keep > 0 else []
    for path in old:
        path.unlink()
    return old


def snapshot(settings: Settings, target: Path) -> Path:
    """A consistent copy for replication, written atomically."""
    target = Path(target)
    tmp = target.with_name(f".{target.name}.part")
    conn = db.connect(settings.db_path)
    try:
        db.backup_to(conn, tmp)
    finally:
        conn.close()
    check = sqlite3.connect(str(tmp))
    try:
        check.execute("PRAGMA journal_mode=DELETE")
    finally:
        check.close()
    os.chmod(tmp, 0o640)
    os.replace(tmp, target)
    return target
