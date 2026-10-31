from datetime import datetime, timedelta, timezone

import pytest

from furnace import db
from furnace.service import Furnace

from conftest import leftover_temp_files

T0 = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def furnace(settings):
    f = Furnace(settings)
    f.startup()
    return f


def blocklist(settings):
    return settings.blocklist_path.read_text().splitlines()


def audit_rows(settings, **where):
    conn = db.connect(settings.db_path)
    try:
        sql = "SELECT * FROM audit_log"
        if where:
            sql += " WHERE " + " AND ".join(f"{k}=?" for k in where)
        return [dict(r) for r in conn.execute(sql + " ORDER BY id", tuple(where.values()))]
    finally:
        conn.close()


def test_startup_creates_empty_files(settings):
    Furnace(settings).startup()
    assert settings.blocklist_path.read_text() == ""
    assert settings.domains_path.read_text() == ""


def test_add_ip_stores_fields_and_publishes(furnace, settings):
    r = furnace.add_ip("198.51.100.7", reason="Phishing host", added_by="analyst.one", now=T0)
    assert r.status == "added"
    assert blocklist(settings) == ["198.51.100.7"]
    found = furnace.get_ip("198.51.100.7")
    assert found.status == "found"
    assert found.data["entry"] == {"ip": "198.51.100.7", "added_at": "2026-01-10T12:00:00Z",
                                   "added_by": "analyst.one", "reason": "Phishing host",
                                   "permanent": False,
                                   "expires_at": "2026-02-09T12:00:00Z"}
    assert found.data["record"]["times_blocked"] == 1
    assert audit_rows(settings, action="add", result="added")[0]["incident_id"] == ""


def test_duplicate_keeps_old_date(furnace):
    furnace.add_ip("198.51.100.7", added_by="first", now=T0)
    r = furnace.add_ip("198.51.100.7", added_by="second", now=T0 + timedelta(days=3))
    assert r.status == "already_exists"
    assert "2026-01-10" in r.message
    entry = furnace.get_ip("198.51.100.7").data["entry"]
    assert entry["added_at"] == "2026-01-10T12:00:00Z"
    assert entry["added_by"] == "first"


def test_rejected_requests_are_audited(furnace, settings):
    r = furnace.add_ip("10.0.0.1", added_by="analyst")
    assert (r.status, r.code) == ("rejected", "private_ip")
    rows = audit_rows(settings, result="rejected")
    assert rows[-1]["reason_code"] == "private_ip"
    assert rows[-1]["value"] == "10.0.0.1"
    assert blocklist(settings) == []


def test_test_ranges_blocked_without_setting(settings):
    settings.allow_test_ranges = False
    f = Furnace(settings)
    f.startup()
    assert f.add_ip("192.0.2.5").code == "documentation_range"


def test_restore_from_archive(furnace, settings):
    furnace.add_ip("203.0.113.50", reason="old", now=T0)
    furnace.remove_ip("203.0.113.50")
    old = furnace.get_ip("203.0.113.50")
    assert old.data["list"] == "archive" and old.data["entry"]["reason"] == "old"

    later = T0 + timedelta(days=60)
    r = furnace.add_ip("203.0.113.50", reason="seen again", now=later)
    assert r.status == "added" and r.data["restored_from_archive"]
    got = furnace.get_ip("203.0.113.50")
    assert got.data["list"] == "active"
    assert got.data["entry"]["added_at"] == db.to_iso(later)
    assert got.data["record"] == {"first_blocked_at": db.to_iso(T0), "last_blocked_at": db.to_iso(later),
                                  "times_blocked": 2, "added_by": "", "reason": "old"}
    conn = db.connect(settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM ip_records").fetchone()[0] == 1  # never a duplicate
    conn.close()
    assert blocklist(settings) == ["203.0.113.50"]


def test_remove_ip(furnace, settings):
    furnace.add_ip("198.51.100.8")
    assert furnace.remove_ip("198.51.100.8").status == "removed"
    assert blocklist(settings) == []
    assert furnace.remove_ip("198.51.100.8").status == "not_found"
    assert furnace.get_ip("198.51.100.8").data["list"] == "archive"
    assert furnace.get_ip("198.51.100.9").status == "not_found"


def test_ip_limit(furnace, settings):
    settings.max_ips = 2
    furnace.add_ip("198.51.100.1")
    furnace.add_ip("198.51.100.2")
    r = furnace.add_ip("198.51.100.3")
    assert r.code == "limit_reached"
    assert len(blocklist(settings)) == 2


def test_domains(furnace, settings):
    r = furnace.add_domain("Bad.Example.COM", reason="C2", added_by="a")
    assert r.status == "added"
    assert settings.domains_path.read_text() == "bad.example.com\n"
    assert furnace.add_domain("bad.example.com").status == "already_exists"
    assert furnace.add_domain("*.example.com").code == "wildcard_not_allowed"
    assert furnace.add_domain("https://bad.example.com/x").code == "url_not_allowed"
    assert furnace.add_domain("198.51.100.1").code == "ip_not_allowed"
    assert furnace.get_domain("BAD.example.com").status == "found"
    assert furnace.remove_domain("bad.example.com").status == "removed"
    assert settings.domains_path.read_text() == ""
    assert furnace.remove_domain("bad.example.com").status == "not_found"


def test_whitelisted_ip_refused(furnace):
    furnace.add_whitelist_ip("198.51.100.0/24", reason="partner")
    r = furnace.add_ip("198.51.100.77")
    assert r.code == "whitelisted"
    assert "198.51.100.0/24" in r.message
    assert furnace.get_ip("198.51.100.77").data["whitelisted_by"] == "198.51.100.0/24"


def test_whitelist_add_removes_covered_ips(furnace, settings):
    for ip in ("203.0.113.1", "203.0.113.2", "198.51.100.9"):
        furnace.add_ip(ip)
    r = furnace.add_whitelist_ip("203.0.113.0/30", added_by="admin")
    assert r.status == "added"
    assert r.data["removed_from_blocklist"] == ["203.0.113.1", "203.0.113.2"]
    assert blocklist(settings) == ["198.51.100.9"]
    assert len(audit_rows(settings, result="removed_by_whitelist")) == 2
    assert furnace.add_whitelist_ip("203.0.113.0/30").status == "already_exists"
    assert furnace.add_whitelist_ip("10.0.0.0/7").code == "range_too_wide"


def test_whitelist_remove_and_list(furnace):
    furnace.add_whitelist_ip("198.51.100.0/24")
    furnace.add_whitelist_ip("2001:db8::/32")
    listed = furnace.list_whitelist_ips()
    assert listed.data["total"] == 2
    assert [e["cidr"] for e in listed.data["entries"]] == ["198.51.100.0/24", "2001:db8::/32"]
    assert furnace.remove_whitelist_ip("198.51.100.0/24").status == "removed"
    assert furnace.remove_whitelist_ip("198.51.100.0/24").status == "not_found"
    assert furnace.add_ip("198.51.100.5").status == "added"


def test_bulk_whitelist_ips_dry_run_then_real(furnace, settings):
    furnace.add_ip("198.51.100.10")
    furnace.add_ip("203.0.113.10")
    entries = ["198.51.100.0/24", "192.0.2.0/24", "bogus", "10.0.0.0/4", "198.51.100.0/24"]
    dry = furnace.bulk_whitelist_ips(entries, dry_run=True)
    assert dry.data["added"] == 2
    assert [x["reason_code"] for x in dry.data["rejected"]] == ["invalid_cidr", "range_too_wide"]
    assert dry.data["removed_from_blocklist"] == ["198.51.100.10"]
    assert furnace.list_whitelist_ips().data["total"] == 0
    assert blocklist(settings) == ["198.51.100.10", "203.0.113.10"]

    real = furnace.bulk_whitelist_ips(entries, added_by="admin")
    assert real.data["added"] == 2
    assert blocklist(settings) == ["203.0.113.10"]
    again = furnace.bulk_whitelist_ips(entries)
    assert again.data["added"] == 0 and again.data["already_exists"] == 2


def test_domain_whitelist(furnace, settings):
    furnace.add_domain("bad.example.com")
    furnace.add_domain("worse.example.net")
    r = furnace.add_whitelist_domain("bad.example.com")
    assert r.data["removed_from_blocklist"] == ["bad.example.com"]
    assert settings.domains_path.read_text() == "worse.example.net\n"
    assert furnace.add_domain("bad.example.com").code == "whitelisted"
    # exact only: a sub-domain is still allowed
    assert furnace.add_domain("x.bad.example.com").status == "added"
    bulk = furnace.bulk_whitelist_domains(["worse.example.net", "*.example.org", "good.example.org"])
    assert bulk.data["added"] == 2 and bulk.data["removed_from_blocklist"] == ["worse.example.net"]
    assert furnace.list_whitelist_domains().data["total"] == 3
    assert furnace.remove_whitelist_domain("bad.example.com").status == "removed"


def test_follower_refuses_changes(settings, tmp_path):
    role = tmp_path / "role"
    role.write_text("follower")
    settings.role_file = role
    f = Furnace(settings)
    f.startup()
    assert f.add_ip("198.51.100.1").code == "not_leader"
    assert f.remove_ip("198.51.100.1").code == "not_leader"
    assert f.add_domain("example.com").code == "not_leader"
    assert f.add_whitelist_ip("198.51.100.0/24").code == "not_leader"
    assert f.bulk_whitelist_domains(["example.com"]).code == "not_leader"
    assert f.get_ip("198.51.100.1").status == "not_found"  # reading still works
    role.write_text("leader")
    assert f.add_ip("198.51.100.1").status == "added"


def test_no_temp_files_left(furnace, settings):
    for i in range(1, 30):
        furnace.add_ip(f"198.51.100.{i}")
        furnace.add_domain(f"host{i}.example.com")
    assert leftover_temp_files(settings.edl_dir, settings.archive_dir) == []
