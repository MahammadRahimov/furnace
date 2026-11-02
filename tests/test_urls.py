"""The URL blocklist (version 0.8). User decisions 2026-11-02: URLs never expire, wildcards are
allowed (not too wide ones), http:// and https:// are removed, and a domain on the whitelist
protects itself and all its subdomains from URL blocks."""

from __future__ import annotations

import sqlite3

import pytest

from furnace import admin, db, edl
from furnace.api import create_app
from furnace.importer import import_urls
from furnace.replica import apply_snapshot
from furnace.service import Furnace
from furnace.validation import Rejected, host_touches_domain, normalize_url

from conftest import ALL_TOKEN, BLOCK_TOKEN, WHITELIST_TOKEN

BLOCK = {"Authorization": f"Bearer {BLOCK_TOKEN}"}
WHITE = {"Authorization": f"Bearer {WHITELIST_TOKEN}"}
ALL = {"Authorization": f"Bearer {ALL_TOKEN}"}


@pytest.fixture
def client(settings):
    return create_app(settings).test_client()


def published(settings) -> list[str]:
    return settings.urls_path.read_text().splitlines()


# ---------------------------------------------------------------------- the URL check

@pytest.mark.parametrize("given, stored", [
    ("bad.example.com", "bad.example.com/"),
    ("https://bad.example.com", "bad.example.com/"),
    ("http://Bad.Example.COM/Login.php", "bad.example.com/login.php"),
    ("HTTPS://bad.example.com/a/b?x=1&y=2#top", "bad.example.com/a/b?x=1&y=2"),
    ("bad.example.com?x=1", "bad.example.com/?x=1"),
    ("bad.example.com.", "bad.example.com/"),
    ("bad.example.com:8080", "bad.example.com:8080/"),
    ("https://bad.example.com:0443/x", "bad.example.com:443/x"),
    ("*.bad.example.com", "*.bad.example.com/"),
    ("^.bad.example.com/", "^.bad.example.com/"),
    ("a.*.example.com/x", "a.*.example.com/x"),
    ("bad.example.com/*/login", "bad.example.com/*/login"),
    ("bad.example.com/?id=*", "bad.example.com/?id=*"),
    ("*.example.co.uk/", "*.example.co.uk/"),
    ("http://203.0.113.5/x", "203.0.113.5/x"),
    ("bücher.example/", "xn--bcher-kva.example/"),
    ("  bad.example.com/a,b;c  ", "bad.example.com/a,b;c"),
])
def test_urls_are_cleaned(given, stored):
    assert normalize_url(given, allow_test_ranges=True).url == stored


@pytest.mark.parametrize("given, code", [
    ("", "missing_value"),
    (None, "missing_value"),
    ("ftp://bad.example.com/", "scheme_not_allowed"),
    ("javascript://x", "scheme_not_allowed"),
    ("https://user:pw@bad.example.com/", "login_not_allowed"),
    ("bad example.com", "invalid_url"),
    ("https:///x", "invalid_url"),
    ("localhost/", "invalid_url"),
    ("bad.example.com:99999/", "invalid_url"),
    ("bad_host.example.com/", "invalid_url"),
    ("*/", "wildcard_too_wide"),
    ("*.com/", "wildcard_too_wide"),
    ("^.com/", "wildcard_too_wide"),
    ("example.*/", "wildcard_too_wide"),
    ("bad.*.com/", "wildcard_too_wide"),
    ("*.co.uk/", "wildcard_too_wide"),
    ("*.com.az/", "wildcard_too_wide"),
    ("*example.com/", "wildcard_partial"),
    ("bad.example.com/log*in", "wildcard_partial"),
    ("[2001:db8::1]/", "ipv6_url_not_allowed"),
    ("10.0.0.1/", "private_ip"),
    ("bad.example.com/" + "a" * 260, "url_too_long"),
    ("bad.example.com/çay", "invalid_url"),
])
def test_bad_urls_are_refused(given, code):
    with pytest.raises(Rejected) as error:
        normalize_url(given)
    assert error.value.code == code


@pytest.mark.parametrize("host, domain, touches", [
    ("x.example.com", "x.example.com", True),          # the domain itself
    ("www.x.example.com", "x.example.com", True),      # a subdomain
    ("*.x.example.com", "x.example.com", True),        # only subdomains of it
    ("*.example.com", "x.example.com", True),          # covers x.example.com
    ("^.example.com", "x.example.com", True),
    ("a.*.example.com", "x.example.com", True),        # a.x.example.com
    ("example.com", "x.example.com", False),           # the parent only: example.com/ does not match x.
    ("y.example.com", "x.example.com", False),
    ("^.example.com", "a.b.example.com", False),       # ^ is one part, a.b is two
    ("*.example.org", "x.example.com", False),
    ("xx.example.com", "x.example.com", False),
])
def test_whitelist_rule(host, domain, touches):
    assert host_touches_domain(host, domain) is touches


# ---------------------------------------------------------------------- API

def test_block_look_up_and_unblock(client, settings):
    r = client.post("/api/v1/urls", json={"url": "https://Bad.Example.com/Phish/", "reason": "phishing",
                                          "added_by": "soar"}, headers=BLOCK)
    assert r.status_code == 201, r.get_json()
    a = r.get_json()
    assert a["entry"]["url"] == "bad.example.com/phish/"
    assert a["given"] == "https://Bad.Example.com/Phish/"
    assert published(settings) == ["bad.example.com/phish/"]

    r = client.post("/api/v1/urls", json={"url": "http://bad.example.com/phish/"}, headers=BLOCK)
    assert (r.status_code, r.get_json()["status"]) == (200, "already_exists")

    r = client.get("/api/v1/urls", query_string={"url": "https://bad.example.com/phish/"}, headers=BLOCK)
    assert r.status_code == 200 and r.get_json()["list"] == "active"
    assert r.get_json()["entry"]["reason"] == "phishing"

    r = client.delete("/api/v1/urls", query_string={"url": "bad.example.com/phish/", "removed_by": "soar"},
                      headers=BLOCK)
    assert r.status_code == 200, r.get_json()
    assert published(settings) == []
    r = client.get("/api/v1/urls?url=bad.example.com/phish/", headers=BLOCK)
    assert r.get_json()["list"] == "archive"
    r = client.delete("/api/v1/urls", json={"url": "bad.example.com/phish/"}, headers=BLOCK)
    assert r.status_code == 404


def test_url_calls_need_the_blocklist_permission(client):
    assert client.post("/api/v1/urls", json={"url": "bad.example.com"}, headers=WHITE).status_code == 401
    assert client.get("/api/v1/urls?url=bad.example.com", headers=WHITE).status_code == 401
    assert client.get("/api/v1/urls?url=bad.example.com").status_code == 401


def test_bad_requests(client):
    r = client.post("/api/v1/urls", json={"url": "*.com/"}, headers=BLOCK)
    assert (r.status_code, r.get_json()["reason_code"]) == (422, "wildcard_too_wide")
    assert client.get("/api/v1/urls", headers=BLOCK).status_code == 400
    r = client.post("/api/v1/urls", json={"url": "bad.example.com", "incident_id": "1"}, headers=BLOCK)
    assert r.status_code == 400


def test_urls_never_expire(settings):
    furnace = Furnace(settings)
    old = db.from_iso("2020-01-01T00:00:00Z")
    assert furnace.add_url("old.example.com/x", now=old).status == "added"
    from furnace.cleanup import run_cleanup
    run_cleanup(settings, force=True)
    assert published(settings) == ["old.example.com/x"]


def test_the_limit(settings):
    settings.max_urls = 1
    furnace = Furnace(settings)
    assert furnace.add_url("a.example.com").status == "added"
    r = furnace.add_url("b.example.com")
    assert (r.status, r.code) == ("rejected", "limit_reached")


def test_follower_refuses(settings, tmp_path):
    role = tmp_path / "role"
    role.write_text("follower")
    settings.role_file = role
    r = Furnace(settings).add_url("bad.example.com")
    assert r.code == "not_leader"


# ---------------------------------------------------------------------- whitelist

def test_whitelisted_domain_protects_itself_and_subdomains(client):
    assert client.post("/api/v1/whitelist/domains", json={"domain": "good.example.com"},
                       headers=WHITE).status_code == 201
    for url in ("good.example.com/", "https://good.example.com/login", "www.good.example.com/x",
                "*.good.example.com/", "*.example.com/", "^.example.com/a"):
        r = client.post("/api/v1/urls", json={"url": url}, headers=BLOCK)
        assert (r.status_code, r.get_json()["reason_code"]) == (422, "whitelisted"), url
        assert "good.example.com" in r.get_json()["message"]
    for url in ("example.com/", "bad.example.com/", "notgood.example.com/"):
        assert client.post("/api/v1/urls", json={"url": url}, headers=BLOCK).status_code == 201, url
    r = client.get("/api/v1/urls?url=www.good.example.com/", headers=BLOCK)
    assert r.get_json()["whitelisted_by"] == "good.example.com"


def test_whitelisting_removes_blocked_urls(client, settings):
    for url in ("good.example.com/a", "www.good.example.com/", "*.example.com/x", "bad.example.com/",
                "203.0.113.7/x", "198.51.100.9/"):
        assert client.post("/api/v1/urls", json={"url": url}, headers=BLOCK).status_code == 201, url
    r = client.post("/api/v1/whitelist/domains", json={"domain": "good.example.com"}, headers=WHITE)
    a = r.get_json()
    assert a["urls_removed_from_blocklist"] == ["*.example.com/x", "good.example.com/a", "www.good.example.com/"]
    assert "3 blocked URLs were removed" in a["message"]
    r = client.post("/api/v1/whitelist/ips", json={"entry": "203.0.113.0/24"}, headers=WHITE)
    assert r.get_json()["urls_removed_from_blocklist"] == ["203.0.113.7/x"]
    assert published(settings) == ["198.51.100.9/", "bad.example.com/"]
    r = client.post("/api/v1/urls", json={"url": "203.0.113.8/"}, headers=BLOCK)
    assert r.get_json()["reason_code"] == "whitelisted"


def test_bulk_whitelist_dry_run_shows_urls(client, settings):
    client.post("/api/v1/urls", json={"url": "a.good.example.com/"}, headers=BLOCK)
    r = client.post("/api/v1/whitelist/domains/bulk", json={"entries": ["good.example.com"], "dry_run": True},
                    headers=WHITE)
    assert r.get_json()["urls_removed_from_blocklist"] == ["a.good.example.com/"]
    assert published(settings) == ["a.good.example.com/"]
    r = client.post("/api/v1/whitelist/domains/bulk", json={"entries": ["good.example.com"]}, headers=WHITE)
    assert r.get_json()["urls_removed_from_blocklist"] == ["a.good.example.com/"]
    assert published(settings) == []


# ---------------------------------------------------------------------- files

def test_bulk_upload(client, settings):
    Furnace(settings).add_whitelist_domain("good.example.com")
    text = ("url\nhttps://bad.example.com/a phishing page\nbad.example.com/a\nhttp://BAD.example.com/a\n"
            "*.evil.example.org\tmalware\nftp://x.example.com/\n*.com/\nwww.good.example.com/x\n"
            "# a comment\nbad.example.net/a,b\n")
    r = client.post("/api/v1/urls/bulk?dry_run=true", data=text, headers=BLOCK, content_type="text/plain")
    a = r.get_json()
    assert r.status_code == 200, a
    assert (a["dry_run"], a["added"], a["skipped"]) == (True, 3, 6)
    assert {s["reason_code"] for s in a["skipped_lines"]} == {"header", "duplicate_in_file", "scheme_not_allowed",
                                                              "wildcard_too_wide", "whitelisted"}
    assert published(settings) == []
    a = client.post("/api/v1/urls/bulk?added_by=soar", data=text, headers=BLOCK, content_type="text/plain").get_json()
    assert a["added"] == 3
    assert published(settings) == ["*.evil.example.org/", "bad.example.com/a", "bad.example.net/a,b"]
    r = Furnace(settings).get_url("bad.example.com/a")
    assert (r.data["entry"]["reason"], r.data["entry"]["added_by"]) == ("phishing page", "soar")
    r = Furnace(settings).get_url("bad.example.net/a,b")
    assert r.data["entry"]["reason"] == "uploaded through the API"


def test_import_command_export_and_archive(settings, tmp_path, capsys):
    source = tmp_path / "urls.txt"
    source.write_text("https://bad.example.com/x\n*.evil.example.org/\n")
    report = import_urls(settings, source)
    assert (report.added_active, len(report.skipped)) == (2, 0)
    assert report.backup_file
    conn = db.connect(settings.db_path)
    try:
        assert edl.write_archive_csv(conn, settings) == 2
    finally:
        conn.close()
    archive = settings.archive_csv_path.read_text()
    assert "url,*.evil.example.org/," in archive and "url,bad.example.com/x," in archive
    report = import_urls(settings, source)
    assert {s.reason for s in report.skipped} == {"already_active"}


def test_cli_import_and_stats(settings, tmp_path, capsys, monkeypatch):
    for key, value in (("FURNACE_DATA_DIR", settings.data_dir), ("FURNACE_EDL_DIR", settings.edl_dir),
                       ("FURNACE_ARCHIVE_DIR", settings.archive_dir), ("FURNACE_BACKUP_DIR", settings.backup_dir),
                       ("FURNACE_AUTH_DIR", settings.auth_dir), ("FURNACE_ALLOW_TEST_RANGES", "true")):
        monkeypatch.setenv(key, str(value))
    monkeypatch.setenv("FURNACE_CONFIG", str(tmp_path / "none.env"))
    source = tmp_path / "urls.txt"
    source.write_text("bad.example.com/x\n")
    assert admin.main(["import-urls", str(source)]) == 0
    assert admin.main(["export", "urls"]) == 0
    assert admin.main(["stats"]) == 0
    out = capsys.readouterr().out
    assert "bad.example.com/x," in out
    assert "urls txt bytes" in out


def test_snapshot_from_a_leader_without_url_tables(settings, tmp_path):
    """A 0.7 leader sends a database without the URL tables: the follower adds them and publishes an empty list."""
    old = tmp_path / "old.db"
    conn = sqlite3.connect(old)
    conn.executescript(db.SCHEMA + "DROP TABLE blocked_urls; DROP TABLE url_records;")
    conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '5')")
    conn.commit()
    conn.close()
    assert "blocked_urls" not in {r[0] for r in sqlite3.connect(old).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    role = tmp_path / "role"
    role.write_text("follower")
    settings.role_file = role
    counts = apply_snapshot(settings, old)
    assert counts["urls"] == 0
    assert settings.urls_path.read_text() == ""


def test_health_counts_urls(client):
    client.post("/api/v1/urls", json={"url": "bad.example.com"}, headers=ALL)
    assert client.get("/health").get_json()["urls"] == 1
