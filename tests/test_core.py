import ipaddress
import threading
import time

from furnace import db, edl
from furnace.config import ConfigError, Settings
from furnace.locking import FileLock, LockTimeout
from furnace.rangeset import RangeSet

from conftest import leftover_temp_files


def test_rangeset_merges_and_finds():
    rs = RangeSet(ipaddress.ip_network(n) for n in
                  ("198.51.100.0/25", "198.51.100.128/25", "203.0.113.8/29", "2001:db8::/32"))
    assert len(rs) == 3
    assert "198.51.100.200" in rs
    assert "203.0.113.15" in rs
    assert "203.0.113.16" not in rs
    assert "192.0.2.1" not in rs
    assert "2001:db8::5" in rs
    assert "2001:db9::5" not in rs
    assert "0.0.0.0" not in RangeSet()


def test_rangeset_is_fast():
    # Only documentation space: 5,000 ranges and 30,000 IPs inside 2001:db8::/32.
    nets = [ipaddress.ip_network(f"2001:db8:{n * 3:x}::/48") for n in range(5000)]
    rs = RangeSet(nets)
    ips = [ipaddress.ip_address(f"2001:db8:{n:x}::1") for n in range(30000)]
    start = time.perf_counter()
    hits = sum(1 for ip in ips if ip in rs)
    assert hits > 0
    assert time.perf_counter() - start < 1.0, "30,000 lookups must take well under a second"


def test_wal_and_synchronous_full(settings):
    conn = db.connect(settings.db_path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    conn.close()


def test_transaction_rolls_back(conn):
    try:
        with db.transaction(conn):
            conn.execute("INSERT INTO blocked_domains(domain, added_at) VALUES ('example.com', 'x')")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert conn.execute("SELECT COUNT(*) FROM blocked_domains").fetchone()[0] == 0


def test_ensure_files_creates_empty_edls(settings):
    edl.ensure_files(settings)
    assert settings.blocklist_path.read_text() == ""
    assert settings.domains_path.read_text() == ""


def test_rebuild_writes_sorted_files_and_no_temp_files(settings, conn):
    with db.transaction(conn):
        for ip, v in (("203.0.113.9", 4), ("198.51.100.200", 4), ("198.51.100.30", 4)):
            conn.execute("INSERT INTO blocked_ips(ip, version, added_at, aging_from)"
                         " VALUES (?,?,'t','t')", (ip, v))
        conn.execute("INSERT INTO blocked_domains(domain, added_at) VALUES ('b.example.com','t')")
        conn.execute("INSERT INTO blocked_domains(domain, added_at) VALUES ('a.example.com','t')")
    counts = edl.rebuild_all(conn, settings)
    assert counts == {"ips": 3, "domains": 2, "archived": 0}
    assert settings.blocklist_path.read_text() == "198.51.100.30\n198.51.100.200\n203.0.113.9\n"
    assert settings.domains_path.read_text() == "a.example.com\nb.example.com\n"
    assert settings.archive_csv_path.read_text().startswith("ip,added_at,archived_at")
    assert leftover_temp_files(settings.edl_dir, settings.archive_dir) == []


def test_atomic_write_cleans_up_on_error(settings, monkeypatch):
    import os
    def broken_replace(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", broken_replace)
    try:
        edl.atomic_write(settings.blocklist_path, "198.51.100.1\n")
    except OSError:
        pass
    assert leftover_temp_files(settings.edl_dir) == []


def test_lock_blocks_second_holder(settings):
    order = []
    with FileLock(settings.lock_path):
        def other():
            with FileLock(settings.lock_path):
                order.append("second")
        t = threading.Thread(target=other)
        t.start()
        time.sleep(0.2)
        order.append("first")
    t.join()
    assert order == ["first", "second"]


def test_lock_timeout(settings):
    with FileLock(settings.lock_path):
        try:
            with FileLock(settings.lock_path, timeout=0.2):
                raise AssertionError("must not get the lock")
        except LockTimeout:
            pass


def test_settings_from_env_and_tokens():
    s = Settings.from_env({"FURNACE_DATA_DIR": "/srv/f", "FURNACE_ARCHIVE_AFTER_DAYS": "45",
                           "FURNACE_BLOCK_TOKEN": "a" * 20})
    assert s.archive_after_days == 45
    assert str(s.edl_dir) == "/srv/f/edl"
    assert s.effective_whitelist_token == "a" * 20
    s.check_tokens()
    for env in ({}, {"FURNACE_BLOCK_TOKEN": "short"},
                {"FURNACE_BLOCK_TOKEN": "a" * 20, "FURNACE_WHITELIST_TOKEN": "short"}):
        try:
            Settings.from_env(env).check_tokens()
            raise AssertionError("must refuse")
        except ConfigError:
            pass
    try:
        Settings.from_env({"FURNACE_ARCHIVE_AFTER_DAYS": "zero"})
        raise AssertionError("must refuse")
    except ConfigError:
        pass


def test_role_file(tmp_path):
    role = tmp_path / "role"
    s = Settings(role_file=role)
    assert not s.is_leader()  # missing file means follower
    role.write_text("leader\n")
    assert s.is_leader()
    role.write_text("follower\n")
    assert not s.is_leader()
    assert Settings().is_leader()
