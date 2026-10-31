"""Version 0.7: permanent IPs and the archive record of every IP and domain ever blocked."""

import csv
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from furnace import admin, backup, db
from furnace.api import create_app
from furnace.cleanup import run_cleanup
from furnace.config import Settings
from furnace.importer import import_domains, import_ips
from furnace.replica import apply_snapshot
from furnace.service import Furnace

from conftest import BLOCK_TOKEN

BLOCK = {"Authorization": f"Bearer {BLOCK_TOKEN}"}
NOW = datetime(2026, 6, 1, 3, 0, tzinfo=timezone.utc)


@pytest.fixture
def furnace(settings):
    f = Furnace(settings)
    f.startup()
    return f


def record(settings, table, key):
    conn = db.connect(settings.db_path)
    try:
        column = "ip" if table == "ip_records" else "domain"
        row = conn.execute(f"SELECT * FROM {table} WHERE {column}=?", (key,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def count(settings, table):
    conn = db.connect(settings.db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------- permanent IPs

def test_permanent_ips_never_expire(furnace, settings):
    old = NOW - timedelta(days=400)
    assert furnace.add_ip("198.51.100.1", permanent=True, now=old).status == "added"
    furnace.add_ip("198.51.100.2", now=old)
    r = run_cleanup(settings, now=NOW)
    assert r.moved == ["198.51.100.2"]
    assert settings.blocklist_path.read_text() == "198.51.100.1\n"
    entry = furnace.get_ip("198.51.100.1").data["entry"]
    assert entry["permanent"] is True and entry["expires_at"] is None
    expired = furnace.get_ip("198.51.100.2")
    assert expired.data["list"] == "archive"  # it left the list, the record stays


def test_permanent_answer_and_message(furnace):
    r = furnace.add_ip("198.51.100.1", permanent=True, now=NOW)
    assert r.message == "The IP is now blocked permanently."
    assert r.data["entry"]["permanent"] is True
    r = furnace.add_ip("198.51.100.2", now=NOW)
    assert r.message == "The IP is now blocked for 30 days."
    assert r.data["entry"]["expires_at"] == db.to_iso(NOW + timedelta(days=30))


def test_same_ip_again_changes_nothing(furnace):
    furnace.add_ip("198.51.100.1", now=NOW)
    r = furnace.add_ip("198.51.100.1", permanent=True, now=NOW + timedelta(days=1))
    assert r.status == "already_exists"
    assert r.data["entry"]["permanent"] is False  # no upgrade: the first block decides
    assert r.data["record"]["times_blocked"] == 1
    furnace.add_ip("198.51.100.2", permanent=True, now=NOW)
    r = furnace.add_ip("198.51.100.2", now=NOW)
    assert r.status == "already_exists" and r.data["entry"]["permanent"] is True


@pytest.mark.parametrize("value", ["yes", "true", 1, 0, [], {}])
def test_permanent_must_be_true_or_false(furnace, settings, value):
    r = furnace.add_ip("198.51.100.1", permanent=value)
    assert (r.status, r.code) == ("rejected", "invalid_permanent")
    assert settings.blocklist_path.read_text() == ""


def test_api_permanent_field(settings):
    c = create_app(settings).test_client()
    r = c.post("/api/v1/ips", json={"ip": "198.51.100.1", "permanent": True}, headers=BLOCK)
    assert r.status_code == 201 and r.get_json()["entry"]["permanent"] is True
    r = c.post("/api/v1/ips", json={"ip": "198.51.100.2", "permanent": None}, headers=BLOCK)
    assert r.status_code == 201 and r.get_json()["entry"]["permanent"] is False
    r = c.post("/api/v1/ips", json={"ip": "198.51.100.3", "permanent": "yes"}, headers=BLOCK)
    assert r.status_code == 422 and r.get_json()["reason_code"] == "invalid_permanent"
    got = c.get("/api/v1/ips/198.51.100.1", headers=BLOCK).get_json()
    assert got["entry"]["permanent"] is True and got["record"]["times_blocked"] == 1


# ---------------------------------------------------------------------- the archive record

def test_every_block_is_recorded_once(furnace, settings):
    furnace.add_ip("198.51.100.1", reason="first", added_by="one", now=NOW)
    assert record(settings, "ip_records", "198.51.100.1")["first_blocked_at"] == db.to_iso(NOW)
    furnace.add_ip("198.51.100.1", now=NOW + timedelta(days=1))  # still in the list: nothing changes
    furnace.remove_ip("198.51.100.1")
    later = NOW + timedelta(days=5)
    r = furnace.add_ip("198.51.100.1", reason="second", now=later)
    assert r.data["restored_from_archive"] is True
    rec = record(settings, "ip_records", "198.51.100.1")
    assert rec == {"ip": "198.51.100.1", "version": 4, "first_blocked_at": db.to_iso(NOW),
                   "last_blocked_at": db.to_iso(later), "times_blocked": 2, "added_by": "one",
                   "reason": "first", "incident_id": ""}  # the column stays, always empty (0.8)
    assert count(settings, "ip_records") == 1


def test_ip_removed_by_whitelist_stays_in_the_archive(furnace, settings):
    furnace.add_ip("198.51.100.1")
    furnace.add_whitelist_ip("198.51.100.0/24")
    assert settings.blocklist_path.read_text() == ""
    assert furnace.get_ip("198.51.100.1").data["list"] == "archive"


def test_domains_never_expire_and_are_recorded(furnace, settings):
    furnace.add_domain("bad.example.com", reason="malware", now=NOW - timedelta(days=400))
    run_cleanup(settings, now=NOW)
    assert settings.domains_path.read_text() == "bad.example.com\n"
    got = furnace.get_domain("bad.example.com")
    assert got.data["list"] == "active" and got.data["record"]["times_blocked"] == 1
    furnace.remove_domain("bad.example.com")
    gone = furnace.get_domain("bad.example.com")
    assert gone.status == "found" and gone.data["list"] == "archive"
    assert gone.data["entry"]["reason"] == "malware"
    r = furnace.add_domain("bad.example.com", now=NOW)
    assert r.data["restored_from_archive"] is True and r.data["record"]["times_blocked"] == 2
    assert furnace.add_domain("bad.example.com").status == "already_exists"
    assert record(settings, "domain_records", "bad.example.com")["times_blocked"] == 2
    assert furnace.get_domain("never.example.com").status == "not_found"


def test_archive_csv_and_export(furnace, settings, tmp_path, monkeypatch):
    furnace.add_ip("198.51.100.10", now=NOW)
    furnace.add_ip("198.51.100.9", now=NOW)
    furnace.remove_ip("198.51.100.9")
    furnace.add_domain("bad.example.com", now=NOW)
    run_cleanup(settings, now=NOW)  # the nightly job writes the CSV
    rows = list(csv.DictReader(settings.archive_csv_path.open()))
    assert [(r["type"], r["value"], r["blocked_now"]) for r in rows] == [
        ("ip", "198.51.100.9", "no"), ("ip", "198.51.100.10", "yes"), ("domain", "bad.example.com", "yes")]

    for key, value in {"FURNACE_DATA_DIR": settings.data_dir, "FURNACE_EDL_DIR": settings.edl_dir,
                       "FURNACE_ARCHIVE_DIR": settings.archive_dir, "FURNACE_BACKUP_DIR": settings.backup_dir,
                       "FURNACE_AUTH_DIR": settings.auth_dir, "FURNACE_CONFIG": tmp_path / "none.env"}.items():
        monkeypatch.setenv(key, str(value))
    out = tmp_path / "archive.csv"
    assert admin.main(["export", "archive", "-o", str(out)]) == 0
    exported = list(csv.DictReader(out.open()))
    assert {(r["type"], r["value"], r["blocked_now"]) for r in exported} == {
        ("ip", "198.51.100.9", "no"), ("ip", "198.51.100.10", "yes"), ("domain", "bad.example.com", "yes")}
    assert admin.main(["export", "ips", "-o", str(out)]) == 0
    assert list(csv.DictReader(out.open()))[0]["permanent"] == "no"


# ---------------------------------------------------------------------- imports

def test_import_permanent(furnace, settings, tmp_path):
    src = tmp_path / "p.txt"
    src.write_text("198.51.100.1,2025-01-01\n198.51.100.2\n")
    r = import_ips(settings, src, permanent=True, now=NOW, report_path=tmp_path / "r.txt")
    assert (r.added_active, r.added_archive) == (2, 0)  # an old date is not too old when permanent
    assert "All added IPs are permanent." in r.summary()
    run_cleanup(settings, now=NOW + timedelta(days=500), force=True)
    assert settings.blocklist_path.read_text() == "198.51.100.1\n198.51.100.2\n"
    assert furnace.get_ip("198.51.100.1").data["entry"]["added_at"] == "2025-01-01T00:00:00Z"


def test_import_domains_writes_the_record(furnace, settings, tmp_path):
    furnace.add_domain("old.example.com", now=NOW)
    furnace.remove_domain("old.example.com")
    src = tmp_path / "d.txt"
    src.write_text("old.example.com\nnew.example.com\n")
    r = import_domains(settings, src, now=NOW, report_path=tmp_path / "r.txt")
    assert (r.added_active, r.restored) == (2, 1)
    assert record(settings, "domain_records", "old.example.com")["times_blocked"] == 2
    assert record(settings, "domain_records", "new.example.com")["times_blocked"] == 1


# ---------------------------------------------------------------------- status

def test_stats_warns_about_many_permanent_ips(furnace, settings, capsys):
    settings.max_ips = 4
    furnace.add_ip("198.51.100.1", permanent=True)
    assert admin.permanent_warnings(settings, 1) == []
    furnace.add_ip("198.51.100.2", permanent=True)
    assert admin.cmd_stats(settings) == 0
    out = capsys.readouterr().out
    assert "permanent ips" in out and "WARNING: 2 permanent IPs use 50 percent of the limit of 4" in out
    settings.max_ips = 0
    assert admin.permanent_warnings(settings, 10000) == []


# ---------------------------------------------------------------------- upgrade from 0.6

SCHEMA_0_6 = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE blocked_ips (ip TEXT PRIMARY KEY, version INTEGER NOT NULL, added_at TEXT NOT NULL,
    aging_from TEXT NOT NULL, added_by TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
    incident_id TEXT NOT NULL DEFAULT '');
CREATE TABLE archived_ips (ip TEXT PRIMARY KEY, version INTEGER NOT NULL, added_at TEXT NOT NULL,
    archived_at TEXT NOT NULL, added_by TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
    incident_id TEXT NOT NULL DEFAULT '');
CREATE TABLE blocked_domains (domain TEXT PRIMARY KEY, added_at TEXT NOT NULL, added_by TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '', incident_id TEXT NOT NULL DEFAULT '');
CREATE TABLE whitelist_ips (cidr TEXT PRIMARY KEY, version INTEGER NOT NULL, start_hex TEXT NOT NULL,
    end_hex TEXT NOT NULL, added_at TEXT NOT NULL, added_by TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '');
CREATE TABLE whitelist_domains (domain TEXT PRIMARY KEY, added_at TEXT NOT NULL, added_by TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '');
CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, action TEXT NOT NULL,
    object_type TEXT NOT NULL, value TEXT NOT NULL DEFAULT '', result TEXT NOT NULL,
    reason_code TEXT NOT NULL DEFAULT '', actor TEXT NOT NULL DEFAULT '', incident_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '', details TEXT NOT NULL DEFAULT '');
INSERT INTO meta VALUES ('schema_version', '3');
INSERT INTO blocked_ips VALUES ('198.51.100.1', 4, '2026-05-20T00:00:00Z', '2026-05-20T00:00:00Z', 'a', 'active', '1');
INSERT INTO archived_ips VALUES ('198.51.100.2', 4, '2026-01-01T00:00:00Z', '2026-02-01T00:00:00Z', 'b', 'old', '2');
INSERT INTO blocked_domains VALUES ('bad.example.com', '2026-03-01T00:00:00Z', 'c', 'domain', '3');
"""


def make_0_6_database(path):
    with sqlite3.connect(path) as c:
        c.executescript(SCHEMA_0_6)


def check_upgraded(s):
    conn = db.connect(s.db_path)
    try:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "5"
        for table in db.INCIDENT_TABLES:  # 0.8: every stored incident_id is deleted
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE incident_id <> ''").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM archived_ips").fetchone()[0] == 0
        ips = {r["ip"]: dict(r) for r in conn.execute("SELECT * FROM ip_records")}
        assert ips["198.51.100.1"]["first_blocked_at"] == "2026-05-20T00:00:00Z"
        assert ips["198.51.100.2"]["reason"] == "old" and ips["198.51.100.2"]["times_blocked"] == 1
        assert conn.execute("SELECT permanent FROM blocked_ips").fetchone()[0] == 0  # old IPs stay temporary
        domain = conn.execute("SELECT * FROM domain_records").fetchone()
        assert (domain["domain"], domain["first_blocked_at"]) == ("bad.example.com", "2026-03-01T00:00:00Z")
    finally:
        conn.close()


def test_upgrade_from_0_6(tmp_path):
    s = Settings(data_dir=tmp_path / "d", edl_dir=tmp_path / "e", archive_dir=tmp_path / "a",
                 backup_dir=tmp_path / "b", auth_dir=tmp_path / "au", allow_test_ranges=True)
    s.ensure_dirs()
    make_0_6_database(s.db_path)
    Furnace(s).startup()
    check_upgraded(s)
    Furnace(s).startup()  # a second start changes nothing
    check_upgraded(s)
    assert Furnace(s).get_ip("198.51.100.2").data["list"] == "archive"


def test_snapshot_from_a_0_6_leader(tmp_path):
    old = tmp_path / "old.db"
    make_0_6_database(old)
    follower = Settings(data_dir=tmp_path / "f", edl_dir=tmp_path / "f/edl", archive_dir=tmp_path / "f/arch",
                        backup_dir=tmp_path / "f/bk", auth_dir=tmp_path / "f/auth", role_file=tmp_path / "role")
    (tmp_path / "role").write_text("follower")
    Furnace(follower).startup()
    assert apply_snapshot(follower, old)["ips"] == 1
    check_upgraded(follower)


def test_snapshot_keeps_permanent_and_records(furnace, settings, tmp_path):
    furnace.add_ip("198.51.100.1", permanent=True)
    furnace.add_ip("198.51.100.2")
    furnace.remove_ip("198.51.100.2")
    snap = backup.snapshot(settings, tmp_path / "snap.db")
    follower = Settings(data_dir=tmp_path / "f", edl_dir=tmp_path / "f/edl", archive_dir=tmp_path / "f/arch",
                        backup_dir=tmp_path / "f/bk", auth_dir=tmp_path / "f/auth", role_file=tmp_path / "role")
    (tmp_path / "role").write_text("follower")
    Furnace(follower).startup()
    apply_snapshot(follower, snap)
    f = Furnace(follower)
    assert f.get_ip("198.51.100.1").data["entry"]["permanent"] is True
    assert f.get_ip("198.51.100.2").data["list"] == "archive"
