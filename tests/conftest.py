"""Shared test setup. Only test ranges (192.0.2.0/24, 198.51.100.0/24,
203.0.113.0/24) and example.com style names are used anywhere in tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from furnace import auth, db
from furnace.config import Settings

BLOCK_TOKEN = "test-block-token-0123456789abcdef"
WHITELIST_TOKEN = "test-whitelist-token-0123456789abcdef"
ALL_TOKEN = "test-all-token-0123456789abcdef"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(
        data_dir=tmp_path / "data",
        edl_dir=tmp_path / "edl",
        archive_dir=tmp_path / "archive",
        backup_dir=tmp_path / "backups",
        auth_dir=tmp_path / "auth",
        allow_test_ranges=True,
        archive_after_days=30,
    )
    s.ensure_dirs()
    db.init_db(s.db_path)
    conn = db.connect(s.db_path)
    try:
        auth.add_token(conn, "block", "block", "test", token=BLOCK_TOKEN)
        auth.add_token(conn, "whitelist", "whitelist", "test", token=WHITELIST_TOKEN)
        auth.add_token(conn, "ui", "all", "test", token=ALL_TOKEN)
    finally:
        conn.close()
    return s


@pytest.fixture
def conn(settings: Settings):
    connection = db.connect(settings.db_path)
    yield connection
    connection.close()


def leftover_temp_files(*folders: Path) -> list[Path]:
    found: list[Path] = []
    for folder in folders:
        found.extend(p for p in folder.iterdir() if p.name.endswith(".tmp"))
    return found
