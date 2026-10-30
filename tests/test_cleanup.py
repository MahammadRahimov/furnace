import csv
from datetime import datetime, timedelta, timezone

import pytest

from furnace import db
from furnace.cleanup import EXIT_SAFETY_STOP, run_cleanup
from furnace.service import Furnace

from conftest import leftover_temp_files

NOW = datetime(2026, 6, 1, 3, 0, tzinfo=timezone.utc)


@pytest.fixture
def furnace(settings):
    f = Furnace(settings)
    f.startup()
    return f


def add_aged(furnace, ip, days_old):
    furnace.add_ip(ip, reason=f"{days_old} days", now=NOW - timedelta(days=days_old))


def counts(settings):
    """(IPs in the list, IPs only in the archive)."""
    conn = db.connect(settings.db_path)
    try:
        return (conn.execute("SELECT COUNT(*) FROM blocked_ips").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM ip_records WHERE ip NOT IN (SELECT ip FROM blocked_ips)")
                .fetchone()[0])
    finally:
        conn.close()


def test_moves_only_old_ips(furnace, settings):
    add_aged(furnace, "198.51.100.1", 31)
    add_aged(furnace, "198.51.100.2", 29)
    add_aged(furnace, "198.51.100.3", 45)
    r = run_cleanup(settings, now=NOW)
    assert r.exit_code == 0
    assert r.moved == ["198.51.100.1", "198.51.100.3"]
    assert settings.blocklist_path.read_text() == "198.51.100.2\n"
    assert counts(settings) == (1, 2)
    rows = list(csv.DictReader(settings.archive_csv_path.open()))
    assert [(r["value"], r["blocked_now"]) for r in rows] == [
        ("198.51.100.1", "no"), ("198.51.100.2", "yes"), ("198.51.100.3", "no")]
    assert rows[0]["reason"] == "31 days"
    assert rows[0]["first_blocked_at"] == db.to_iso(NOW - timedelta(days=31))
    assert leftover_temp_files(settings.edl_dir, settings.archive_dir) == []


def test_archive_after_days_setting(furnace, settings):
    settings.archive_after_days = 7
    add_aged(furnace, "198.51.100.1", 8)
    add_aged(furnace, "198.51.100.2", 6)
    assert run_cleanup(settings, now=NOW).moved == ["198.51.100.1"]


def test_ip_never_in_both_places(furnace, settings):
    add_aged(furnace, "203.0.113.4", 40)
    run_cleanup(settings, now=NOW)
    assert counts(settings) == (0, 1)
    furnace.add_ip("203.0.113.4", now=NOW)
    assert counts(settings) == (1, 0)
    assert run_cleanup(settings, now=NOW).moved == []


def test_dry_run_changes_nothing(furnace, settings):
    add_aged(furnace, "198.51.100.1", 31)
    r = run_cleanup(settings, now=NOW, dry_run=True)
    assert r.moved == ["198.51.100.1"] and "Dry run" in r.message
    assert counts(settings) == (1, 0)
    assert settings.blocklist_path.read_text() == "198.51.100.1\n"


def fill(furnace, old, fresh):
    ips = [f"198.51.100.{i}" for i in range(1, 255)] + [f"203.0.113.{i}" for i in range(1, 255)]
    for ip in ips[:old]:
        add_aged(furnace, ip, 40)
    for ip in ips[old:old + fresh]:
        add_aged(furnace, ip, 1)


def test_safety_stop(furnace, settings):
    fill(furnace, old=21, fresh=79)  # 21 of 100 = 21 percent
    r = run_cleanup(settings, now=NOW)
    assert r.exit_code == EXIT_SAFETY_STOP
    assert "Safety stop" in r.message
    assert counts(settings) == (100, 0)
    conn = db.connect(settings.db_path)
    row = conn.execute("SELECT * FROM audit_log WHERE reason_code='safety_stop'").fetchone()
    conn.close()
    assert row is not None

    forced = run_cleanup(settings, now=NOW, force=True)
    assert forced.exit_code == 0 and len(forced.moved) == 21
    assert counts(settings) == (79, 21)


def test_exactly_twenty_percent_is_allowed(furnace, settings):
    fill(furnace, old=20, fresh=80)
    assert run_cleanup(settings, now=NOW).exit_code == 0
    assert counts(settings) == (80, 20)


def test_small_lists_skip_the_safety_stop(furnace, settings):
    fill(furnace, old=50, fresh=49)  # 99 IPs, below the 100 minimum
    assert run_cleanup(settings, now=NOW).exit_code == 0
    assert counts(settings) == (49, 50)


def test_follower_skips_cleanup(furnace, settings, tmp_path):
    add_aged(furnace, "198.51.100.1", 40)
    role = tmp_path / "role"
    role.write_text("follower")
    settings.role_file = role
    r = run_cleanup(settings, now=NOW)
    assert r.exit_code == 0 and "standby" in r.message
    assert counts(settings) == (1, 0)
