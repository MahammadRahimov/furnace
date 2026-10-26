import csv
import time
from datetime import datetime, timedelta, timezone

import pytest

from furnace import admin, backup, db
from furnace.importer import ImportError_, import_domains, import_ips, import_whitelist
from furnace.replica import ReplicaError, apply_snapshot
from furnace.service import Furnace

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def furnace(settings):
    f = Furnace(settings)
    f.startup()
    return f


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def rows(settings, table):
    conn = db.connect(settings.db_path)
    try:
        return {r[0]: dict(r) for r in conn.execute(f"SELECT * FROM {table}")}
    finally:
        conn.close()


def test_dated_import_uses_real_dates(furnace, settings, tmp_path):
    src = write(tmp_path, "list.csv",
                "ip,date,reason\n"
                "# a comment\n"
                "\n"
                "198.51.100.1,2026-05-20,phishing\n"
                "198.51.100.2;2026-04-01;old one\n"
                "198.51.100.3\t1779926400\ttab and unix time\n"
                "198.51.100.4 2026-05-25 08:30:00 space separated words\n")
    r = import_ips(settings, src, now=NOW, report_path=tmp_path / "r.txt")
    active, archive = rows(settings, "blocked_ips"), rows(settings, "archived_ips")
    assert active["198.51.100.1"]["added_at"] == "2026-05-20T00:00:00Z"
    assert active["198.51.100.1"]["reason"] == "phishing"
    assert "198.51.100.2" in archive  # older than 30 days -> archive
    assert archive["198.51.100.2"]["added_at"] == "2026-04-01T00:00:00Z"
    assert active["198.51.100.3"]["added_at"] == "2026-05-28T00:00:00Z"
    assert active["198.51.100.4"]["added_at"] == "2026-05-25T08:30:00Z"
    assert active["198.51.100.4"]["reason"] == "space separated words"
    assert (r.added_active, r.added_archive) == (3, 1)
    assert [s.reason for s in r.skipped] == ["header"]
    assert settings.blocklist_path.read_text().splitlines() == ["198.51.100.1", "198.51.100.3", "198.51.100.4"]
    assert "198.51.100.2" in settings.archive_csv_path.read_text()
    assert r.backup_file and "furnace-" in r.backup_file


def test_old_ips_fresh_option(furnace, settings, tmp_path):
    src = write(tmp_path, "old.txt", "198.51.100.9,2026-01-01\n")
    import_ips(settings, src, old_ips="fresh", now=NOW, report_path=tmp_path / "r.txt")
    assert "198.51.100.9" in rows(settings, "blocked_ips")
    assert rows(settings, "blocked_ips")["198.51.100.9"]["added_at"] == db.to_iso(NOW)


def test_undated_ips_are_spread_over_the_period(furnace, settings, tmp_path):
    lines = "".join(f"203.0.113.{i}\n" for i in range(1, 61))
    src = write(tmp_path, "plain.txt", lines)
    import_ips(settings, src, now=NOW, report_path=tmp_path / "r.txt")
    active = rows(settings, "blocked_ips")
    assert len(active) == 60
    aging = sorted(db.from_iso(r["aging_from"]) for r in active.values())
    expiry_days = {(a + timedelta(days=30)).date() for a in aging}
    assert len(expiry_days) >= 29, "expiry must be spread over the period, not one night"
    assert min(aging) + timedelta(days=30) > NOW
    assert max(aging) + timedelta(days=30) < NOW + timedelta(days=30)
    assert all(r["added_at"] == db.to_iso(NOW) for r in active.values())
    from furnace.cleanup import run_cleanup
    settings.cleanup_min_size = 10
    for day in range(1, 31):
        assert run_cleanup(settings, now=NOW + timedelta(days=day)).exit_code == 0
    assert len(rows(settings, "blocked_ips")) == 0


def test_skips_are_reported(furnace, settings, tmp_path):
    furnace.add_ip("198.51.100.50")
    furnace.add_whitelist_ip("203.0.113.0/24")
    src = write(tmp_path, "mixed.txt",
                "198.51.100.1\n198.51.100.1\n10.0.0.1\nnot-an-ip\n198.51.100.50\n203.0.113.7\n"
                "198.51.100.0/24\n198.51.100.2,yesterday-ish\n198.51.100.3,2099-01-01\n")
    report_file = tmp_path / "report.txt"
    r = import_ips(settings, src, now=NOW, report_path=report_file)
    reasons = [s.reason for s in r.skipped]
    assert reasons == ["duplicate_in_file", "private_ip", "invalid_ip", "already_active", "whitelisted",
                       "range_not_allowed", "invalid_date", "future_date"]
    text = report_file.read_text()
    assert "2 | duplicate_in_file | 198.51.100.1" in text
    assert "whitelisted" in text
    assert r.added_active == 1


def test_dry_run_changes_nothing(furnace, settings, tmp_path):
    src = write(tmp_path, "list.txt", "198.51.100.1\n198.51.100.2\n")
    r = import_ips(settings, src, dry_run=True, now=NOW, report_path=tmp_path / "r.txt")
    assert r.added_active == 2 and r.dry_run and not r.backup_file
    assert rows(settings, "blocked_ips") == {}
    assert settings.blocklist_path.read_text() == ""
    assert "DRY RUN" in (tmp_path / "r.txt").read_text()


def test_all_or_nothing_on_limit(furnace, settings, tmp_path):
    settings.max_ips = 3
    src = write(tmp_path, "list.txt", "".join(f"198.51.100.{i}\n" for i in range(1, 6)))
    with pytest.raises(ImportError_):
        import_ips(settings, src, now=NOW, report_path=tmp_path / "r.txt")
    assert rows(settings, "blocked_ips") == {}


def test_all_or_nothing_on_database_error(furnace, settings, tmp_path, monkeypatch):
    src = write(tmp_path, "list.txt", "198.51.100.1\n198.51.100.2\n")
    from furnace import importer
    real_audit = importer.db.audit
    def broken(*a, **k):
        raise RuntimeError("disk problem")
    monkeypatch.setattr(importer.db, "audit", broken)
    with pytest.raises(RuntimeError):
        import_ips(settings, src, now=NOW, report_path=tmp_path / "r.txt")
    monkeypatch.setattr(importer.db, "audit", real_audit)
    assert rows(settings, "blocked_ips") == {}


@pytest.mark.parametrize("line, fmt, expected", [
    ("198.51.100.1,31/05/2026", None, "2026-05-31T00:00:00Z"),
    ("198.51.100.1,05/31/2026", None, "2026-05-31T00:00:00Z"),
    ("198.51.100.1,15.05.2026 10:00:00", None, "2026-05-15T10:00:00Z"),
    ("198.51.100.1,2026-05-15T10:00:00Z", None, "2026-05-15T10:00:00Z"),
    ("198.51.100.1,2026-05-15T12:00:00+02:00", None, "2026-05-15T10:00:00Z"),
    ("198.51.100.1,15 May 2026", None, "2026-05-15T00:00:00Z"),
    ("198.51.100.1,1778889600000", None, "2026-05-16T00:00:00Z"),
    ("198.51.100.1,10/05/2026", "%d/%m/%Y", "2026-05-10T00:00:00Z"),
    ("198.51.100.1,05/10/2026", "%m/%d/%Y", "2026-05-10T00:00:00Z"),
    ("198.51.100.1,2026|05|20", "%Y|%m|%d", "2026-05-20T00:00:00Z"),
])
def test_date_formats(furnace, settings, tmp_path, line, fmt, expected):
    src = write(tmp_path, "d.txt", line + "\n")
    import_ips(settings, src, date_format=fmt, now=NOW, report_path=tmp_path / "r.txt")
    assert rows(settings, "blocked_ips")["198.51.100.1"]["added_at"] == expected


def test_ambiguous_dates_are_not_guessed(furnace, settings, tmp_path):
    src = write(tmp_path, "d.txt", "198.51.100.1,05/06/2026\n")
    r = import_ips(settings, src, now=NOW, report_path=tmp_path / "r.txt")
    assert [s.reason for s in r.skipped] == ["ambiguous_date"]
    # the rest of the file tells us the order: 25/05 can only be day-first
    src = write(tmp_path, "d2.txt", "198.51.100.1,03/05/2026\n198.51.100.2,25/05/2026\n")
    import_ips(settings, src, now=NOW, report_path=tmp_path / "r.txt")
    assert rows(settings, "blocked_ips")["198.51.100.1"]["added_at"] == "2026-05-03T00:00:00Z"
    src = write(tmp_path, "d3.txt", "198.51.100.3,25/05/2026\n198.51.100.4,05/25/2026\n")
    with pytest.raises(ImportError_):
        import_ips(settings, src, now=NOW, report_path=tmp_path / "r.txt")


def test_archived_ip_is_restored_by_import(furnace, settings, tmp_path):
    furnace.add_ip("198.51.100.7", now=NOW - timedelta(days=40))
    from furnace.cleanup import run_cleanup
    run_cleanup(settings, now=NOW)
    assert "198.51.100.7" in rows(settings, "archived_ips")
    import_ips(settings, write(tmp_path, "a.txt", "198.51.100.7,2026-05-30\n"), now=NOW,
               report_path=tmp_path / "r.txt")
    assert "198.51.100.7" in rows(settings, "blocked_ips")
    assert "198.51.100.7" not in rows(settings, "archived_ips")


def test_30000_lines_in_seconds(furnace, settings, tmp_path, monkeypatch):
    # The IPv4 test ranges hold only 762 addresses. For volume we use the IPv6
    # documentation range 2001:db8::/32 (RFC 3849), allowed for this test only.
    import ipaddress
    from furnace import importer
    doc6 = ipaddress.ip_network("2001:db8::/32")
    real_validate = importer.validate_ip
    def validate_with_doc6(value, allow_test_ranges=False):
        ip = ipaddress.ip_address(value.strip())
        return ip if ip in doc6 else real_validate(value, allow_test_ranges)
    monkeypatch.setattr(importer, "validate_ip", validate_with_doc6)
    furnace.add_whitelist_ip("2001:db8:0:1::/64")
    settings.max_ips = 50000
    lines = [f"2001:db8::{i:x},2026-05-{(i % 28) + 1:02d},bulk" for i in range(1, 30001)]
    lines[5:5] = ["2001:db8:0:1::5"]  # whitelisted, must be skipped
    src = write(tmp_path, "big.csv", "\n".join(lines) + "\n")
    start = time.perf_counter()
    r = import_ips(settings, src, now=NOW, report_path=tmp_path / "r.txt")
    elapsed = time.perf_counter() - start
    assert r.added_active + r.added_archive == 30000
    assert [s.reason for s in r.skipped] == ["whitelisted"]
    assert elapsed < 15, f"took {elapsed:.1f} seconds"
    print(f"30,000 lines imported in {elapsed:.2f} seconds")


def test_import_domains(furnace, settings, tmp_path):
    furnace.add_whitelist_domain("good.example.com")
    src = write(tmp_path, "d.txt", "domain,reason\nBad.Example.com,c2\nbad.example.com\n*.example.org\n"
                                   "good.example.com\nother.example.net\n")
    r = import_domains(settings, src, now=NOW, report_path=tmp_path / "r.txt")
    assert r.added_active == 2
    assert [s.reason for s in r.skipped] == ["header", "duplicate_in_file", "wildcard_not_allowed", "whitelisted"]
    assert settings.domains_path.read_text() == "bad.example.com\nother.example.net\n"
    assert rows(settings, "blocked_domains")["bad.example.com"]["reason"] == "c2"


def test_import_whitelist(furnace, settings, tmp_path):
    furnace.add_ip("198.51.100.10")
    src = write(tmp_path, "w.txt", "cidr\n198.51.100.0/24\n0.0.0.0/0\n203.0.113.0/25\n")
    dry = import_whitelist(settings, src, "ips", dry_run=True, report_path=tmp_path / "r.txt")
    assert dry.added_active == 2 and settings.blocklist_path.read_text() == "198.51.100.10\n"
    r = import_whitelist(settings, src, "ips", report_path=tmp_path / "r.txt")
    assert r.added_active == 2
    assert "range_too_wide" in [s.reason for s in r.skipped]
    assert settings.blocklist_path.read_text() == ""
    src = write(tmp_path, "wd.txt", "good.example.com\nhttp://x.example.com\n")
    r = import_whitelist(settings, src, "domains", report_path=tmp_path / "r.txt")
    assert r.added_active == 1


def test_backup_keeps_newest(furnace, settings):
    made = [backup.make_backup(settings, keep=3) for _ in range(5)]
    left = sorted(settings.backup_dir.glob("furnace-*.db"))
    assert left == made[-3:]


def test_replica_roundtrip(furnace, settings, tmp_path):
    furnace.add_ip("198.51.100.1")
    furnace.add_domain("bad.example.com")
    snap = backup.snapshot(settings, tmp_path / "snap.db")

    from furnace.config import Settings
    follower = Settings(data_dir=tmp_path / "f", edl_dir=tmp_path / "f/edl", archive_dir=tmp_path / "f/arch",
                        backup_dir=tmp_path / "f/bk", role_file=tmp_path / "role", block_token="x" * 20)
    (tmp_path / "role").write_text("follower")
    Furnace(follower).startup()
    counts = apply_snapshot(follower, snap)
    assert counts["ips"] == 1
    assert follower.blocklist_path.read_text() == "198.51.100.1\n"
    assert follower.domains_path.read_text() == "bad.example.com\n"
    (tmp_path / "role").write_text("leader")
    with pytest.raises(ReplicaError):
        apply_snapshot(follower, snap)
    (tmp_path / "role").write_text("follower")
    bad = write(tmp_path, "bad.db", "this is not a database")
    with pytest.raises(Exception):
        apply_snapshot(follower, bad)
    assert follower.blocklist_path.read_text() == "198.51.100.1\n"


def test_admin_cli(furnace, settings, tmp_path, monkeypatch, capsys):
    env = {"FURNACE_DATA_DIR": str(settings.data_dir), "FURNACE_EDL_DIR": str(settings.edl_dir),
           "FURNACE_ARCHIVE_DIR": str(settings.archive_dir), "FURNACE_BACKUP_DIR": str(settings.backup_dir),
           "FURNACE_ALLOW_TEST_RANGES": "true", "FURNACE_BLOCK_TOKEN": "y" * 24}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("FURNACE_CONFIG", str(tmp_path / "none.env"))
    src = write(tmp_path, "l.txt", "198.51.100.1\n198.51.100.2\n")
    assert admin.main(["import-ips", str(src), "--dry-run", "--report", str(tmp_path / "r.txt")]) == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert admin.main(["import-ips", str(src), "--report", str(tmp_path / "r.txt")]) == 0
    out_csv = tmp_path / "out.csv"
    assert admin.main(["export", "ips", "-o", str(out_csv)]) == 0
    exported = list(csv.DictReader(out_csv.open()))
    assert [r["ip"] for r in exported] == ["198.51.100.1", "198.51.100.2"]
    assert admin.main(["stats"]) == 0
    assert "active ips" in capsys.readouterr().out
    settings.blocklist_path.write_text("garbage\n")
    assert admin.main(["rebuild"]) == 0
    assert settings.blocklist_path.read_text() == "198.51.100.1\n198.51.100.2\n"
    assert admin.main(["cleanup", "--dry-run"]) == 0
    assert admin.main(["backup", "--keep", "2"]) == 0
    assert admin.main(["check-config"]) == 0
    assert "y" * 24 not in capsys.readouterr().out  # never print secrets
    assert admin.main(["import-ips", str(tmp_path / "missing.txt")]) == 2
