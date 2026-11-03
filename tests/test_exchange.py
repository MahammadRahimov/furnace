"""Export a list and upload it again (user decision 2026-11-03).

The main test fills one server, exports every list through the API, uploads the files to an empty second
server through the bulk endpoints, and checks that both databases and list files are the same: dates,
who added it, the reason, the permanent flag, the count-from date and the archive history."""

from __future__ import annotations

import csv
import io
import json
from datetime import timedelta
from pathlib import Path

import pytest

from furnace import auth, db
from furnace.api import create_app
from furnace.cleanup import run_cleanup
from furnace.config import Settings
from furnace.importer import ImportError_, import_domains, import_ips, import_whitelist
from furnace.service import Furnace

from conftest import ALL_TOKEN, BLOCK_TOKEN, WHITELIST_TOKEN

ALL = {"Authorization": f"Bearer {ALL_TOKEN}"}
BLOCK = {"Authorization": f"Bearer {BLOCK_TOKEN}"}
WHITE = {"Authorization": f"Bearer {WHITELIST_TOKEN}"}
NOW = db.utcnow()
ORDER = ("whitelist-ips", "whitelist-domains", "ips", "domains", "urls", "ips-archive", "domains-archive",
         "urls-archive")
BULK = {"ips": "/api/v1/ips/bulk", "ips-archive": "/api/v1/ips/bulk", "domains": "/api/v1/domains/bulk",
        "domains-archive": "/api/v1/domains/bulk", "urls": "/api/v1/urls/bulk", "urls-archive": "/api/v1/urls/bulk",
        "whitelist-ips": "/api/v1/whitelist/ips/bulk", "whitelist-domains": "/api/v1/whitelist/domains/bulk"}


def new_server(folder: Path) -> Settings:
    s = Settings(data_dir=folder / "data", edl_dir=folder / "edl", archive_dir=folder / "archive",
                 backup_dir=folder / "backups", auth_dir=folder / "auth", allow_test_ranges=True,
                 archive_after_days=30)
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


def fill(s: Settings, tmp_path: Path) -> None:
    f = Furnace(s)
    ago = lambda days: NOW - timedelta(days=days)  # noqa: E731
    assert f.add_ip("198.51.100.1", "phishing, wave 2", "soar", now=ago(10)).status == "added"
    assert f.add_ip("198.51.100.2", 'said "never"', "admin.one", now=ago(200), permanent=True).status == "added"
    f.add_ip("198.51.100.3", "first time", "soar", now=ago(80))          # blocked twice, now expired
    f.remove_ip("198.51.100.3", "admin.one", now=ago(70))
    f.add_ip("198.51.100.3", "second time", "soar", now=ago(50))
    f.add_ip("203.0.113.9", "will be whitelisted", "soar", now=ago(5))
    f.add_domain("bad.example.com", "malware; c2", "soar", now=ago(40))
    f.add_domain("gone.example.net", "unblocked later", "soar", now=ago(30))
    f.remove_domain("gone.example.net", "admin.one", now=ago(20))
    f.add_url("https://bad.example.org/login?a=1,2", "url, with comma", "soar", now=ago(15))
    f.add_url("*.evil.example.org/", "wildcard", "soar", now=ago(14))
    f.add_url("old.example.org/x", "unblocked", "soar", now=ago(13))
    f.remove_url("old.example.org/x", "admin.one", now=ago(12))
    f.add_whitelist_ip("203.0.113.0/28", "our office", "admin.one", now=ago(4))   # removes 203.0.113.9
    f.add_whitelist_ip("2001:db8::/32", "ipv6 lab", "admin.one", now=ago(3))
    f.add_whitelist_domain("good.example.com", "our site", "admin.one", now=ago(60))
    undated = tmp_path / "undated.txt"
    undated.write_text("192.0.2.1\n192.0.2.2\n")                          # count-from dates are spread out
    import_ips(s, undated, added_by="old list", reason="from the old firewall", report_path=tmp_path / "r.txt")
    run_cleanup(s, force=True)                                            # 198.51.100.3 goes to the archive


TABLES = {
    "blocked_ips": "SELECT ip, version, added_at, aging_from, added_by, reason, permanent FROM blocked_ips ORDER BY ip",
    "ip_records": "SELECT ip, version, first_blocked_at, last_blocked_at, times_blocked, added_by, reason"
                  " FROM ip_records ORDER BY ip",
    "blocked_domains": "SELECT domain, added_at, added_by, reason FROM blocked_domains ORDER BY domain",
    "domain_records": "SELECT domain, first_blocked_at, last_blocked_at, times_blocked, added_by, reason"
                      " FROM domain_records ORDER BY domain",
    "blocked_urls": "SELECT url, host, added_at, added_by, reason FROM blocked_urls ORDER BY url",
    "url_records": "SELECT url, first_blocked_at, last_blocked_at, times_blocked, added_by, reason"
                   " FROM url_records ORDER BY url",
    "whitelist_ips": "SELECT cidr, version, start_hex, end_hex, added_at, added_by, reason FROM whitelist_ips"
                     " ORDER BY cidr",
    "whitelist_domains": "SELECT domain, added_at, added_by, reason FROM whitelist_domains ORDER BY domain",
}


def snapshot(s: Settings) -> dict[str, list[tuple]]:
    conn = db.connect(s.db_path)
    try:
        return {name: [tuple(r) for r in conn.execute(sql)] for name, sql in TABLES.items()}
    finally:
        conn.close()


def export(client, kind: str, headers=ALL) -> str:
    r = client.get(f"/api/v1/export/{kind}", headers=headers)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.mimetype == "text/csv"
    assert f'filename="furnace-{kind}-' in r.headers["Content-Disposition"]
    return r.get_data(as_text=True)


def upload(client, kind: str, text: str, **args):
    return client.post(BULK[kind], data=text.encode(), content_type="text/plain", headers=ALL, query_string=args)


@pytest.fixture
def servers(tmp_path):
    a, b = new_server(tmp_path / "a"), new_server(tmp_path / "b")
    fill(a, tmp_path)
    return a, b, create_app(a).test_client(), create_app(b).test_client()


def test_everything_moves_to_a_new_server(servers):
    a, b, ca, cb = servers
    files = {kind: export(ca, kind) for kind in ORDER}
    for kind in ORDER:
        r = upload(cb, kind, files[kind])
        assert r.status_code == 200, (kind, r.get_json())
        assert r.get_json()["furnace_export"] == kind
        assert r.get_json()["skipped"] == 0, (kind, r.get_json()["skipped_lines"])
    before, after = snapshot(a), snapshot(b)
    for table in TABLES:
        assert after[table] == before[table], table
    assert len(before["ip_records"]) == 6 and dict((r[0], r[4]) for r in before["ip_records"])["198.51.100.3"] == 2
    for name in ("blocklist_path", "domains_path", "urls_path"):
        assert getattr(b, name).read_text() == getattr(a, name).read_text()
    # The second server exports the same files (only the time in the second line differs).
    for kind in ORDER:
        assert export(cb, kind).splitlines()[2:] == files[kind].splitlines()[2:]


def test_uploading_the_same_file_again_changes_nothing(servers):
    a, b, ca, cb = servers
    files = {kind: export(ca, kind) for kind in ORDER}
    for kind in ORDER:
        upload(cb, kind, files[kind])
    first = snapshot(b)
    for kind in ORDER:
        d = upload(cb, kind, files[kind]).get_json()
        assert d["added"] == 0, kind
        if kind.endswith("-archive"):
            assert (d["archive_added"], d["archive_updated"]) == (0, 0)
    assert snapshot(b) == first


def test_the_export_file(servers):
    a, _, ca, _ = servers
    text = export(ca, "ips")
    lines = text.splitlines()
    assert lines[0] == "# furnace-export ips v1"
    assert lines[1].startswith("# Furnace ") and "/api/v1/ips/bulk" in lines[1]
    assert lines[2] == "ip,added_at,added_by,reason,permanent,counted_from"
    rows = {r["ip"]: r for r in csv.DictReader(io.StringIO("\n".join(lines[2:])))}
    assert rows["198.51.100.1"]["reason"] == "phishing, wave 2"         # a comma stays inside the field
    assert rows["198.51.100.2"]["permanent"] == "yes"
    assert rows["192.0.2.1"]["counted_from"] != rows["192.0.2.1"]["added_at"]
    archive = export(ca, "ips-archive").splitlines()
    assert archive[2] == "ip,first_blocked_at,last_blocked_at,times_blocked,blocked_now,added_by,reason"


def test_json_export(servers):
    _, _, ca, _ = servers
    d = ca.get("/api/v1/export/ips?format=json", headers=BLOCK).get_json()
    assert d["list"] == "ips" and d["count"] == len(d["entries"]) == 4
    assert {e["ip"]: e["permanent"] for e in d["entries"]}["198.51.100.2"] is True
    arch = ca.get("/api/v1/export/domains-archive?format=json", headers=BLOCK).get_json()["entries"]
    assert {e["domain"]: e["blocked_now"] for e in arch} == {"bad.example.com": True, "gone.example.net": False}
    assert ca.get("/api/v1/export/ips?format=xml", headers=BLOCK).status_code == 400


def test_export_permissions(servers):
    _, _, ca, _ = servers
    assert ca.get("/api/v1/export/ips", headers=BLOCK).status_code == 200
    assert ca.get("/api/v1/export/ips", headers=WHITE).status_code == 401
    assert ca.get("/api/v1/export/whitelist-ips", headers=WHITE).status_code == 200
    assert ca.get("/api/v1/export/whitelist-domains", headers=BLOCK).status_code == 401
    assert ca.get("/api/v1/export/ips").status_code == 401
    r = ca.get("/api/v1/export/audit", headers=ALL)
    assert r.status_code == 404 and "ips-archive" in r.get_json()["message"]


def test_the_follower_exports_too(servers, tmp_path):
    a, _, _, _ = servers
    role = tmp_path / "role"
    role.write_text("follower")
    a.role_file = role
    c = create_app(a).test_client()
    assert c.get("/health").get_json()["role"] == "follower"
    assert export(c, "domains").splitlines()[3].startswith("bad.example.com,")
    assert upload(c, "domains", export(c, "domains")).status_code == 503   # uploads only on the leader


def test_wrong_files_are_refused(servers):
    _, b, ca, cb = servers
    ips = export(ca, "ips")
    r = upload(cb, "domains", ips)
    assert r.status_code == 422 and "/api/v1/ips/bulk" in r.get_json()["message"]
    r = upload(cb, "whitelist-domains", export(ca, "whitelist-ips"))
    assert r.status_code == 422 and "/api/v1/whitelist/ips/bulk" in r.get_json()["message"]
    no_ip = ips.replace("ip,added_at", "address,added_at")
    r = upload(cb, "ips", no_ip)
    assert r.status_code == 422 and "no column ip" in r.get_json()["message"]
    r = upload(cb, "ips", ips.replace("ips v1", "ips v9"))
    assert r.status_code == 422 and "v9" in r.get_json()["message"]
    r = upload(cb, "ips", ips.replace("ips v1", "lists v1"))
    assert r.status_code == 422 and "unknown list type" in r.get_json()["message"]
    r = upload(cb, "ips", ips, permanent="true")
    assert r.status_code == 422 and "Remove the option permanent" in r.get_json()["message"]
    assert snapshot(b)["blocked_ips"] == []


def test_bad_rows_are_skipped_and_reported(servers):
    _, b, _, cb = servers
    text = ("# furnace-export ips v1\nip,added_at,permanent\n"
            "198.51.100.20,2026-13-45T00:00:00Z,no\n"        # no such date
            "198.51.100.21,2099-01-01T00:00:00Z,no\n"        # in the future
            "198.51.100.22,%s,maybe\n"                       # not yes or no
            "10.0.0.1,%s,no\n"                               # private
            "198.51.100.23,%s,no\n198.51.100.23,%s,no\n"     # twice
            % ((db.to_iso(NOW),) * 4))
    d = upload(cb, "ips", text).get_json()
    assert d["added"] == 1
    assert [s["reason_code"] for s in d["skipped_lines"]] == [
        "invalid_date", "future_date", "invalid_permanent", "private_ip", "duplicate_in_file"]
    assert [r[0] for r in snapshot(b)["blocked_ips"]] == ["198.51.100.23"]


def test_a_spreadsheet_saved_file_is_read(servers):
    """Semicolons, quotes around the marker, another date form, extra and reordered columns."""
    _, b, _, cb = servers
    text = ('"# furnace-export domains v1";;;\n'
            "reason;domain;note;added_at\n"
            "from excel;bad.example.com;x;2026-10-01 10:30\n")
    d = upload(cb, "domains", text).get_json()
    assert d["added"] == 1, d
    assert snapshot(b)["blocked_domains"] == [("bad.example.com", "2026-10-01T10:30:00Z", "", "from excel")]


def test_old_ips_go_to_the_archive_only(servers):
    """An IP whose 30 days are over on the old server is not blocked again on the new one."""
    _, b, _, cb = servers
    old = db.to_iso(NOW - timedelta(days=45))
    d = upload(cb, "ips", f"# furnace-export ips v1\nip,added_at,permanent\n198.51.100.30,{old},no\n"
                          f"198.51.100.31,{old},yes\n").get_json()
    assert (d["added"], d["archived"]) == (1, 1)
    snap = snapshot(b)
    assert [r[0] for r in snap["blocked_ips"]] == ["198.51.100.31"]
    assert [r[0] for r in snap["ip_records"]] == ["198.51.100.30", "198.51.100.31"]


def test_whitelist_file_removes_what_it_protects(servers):
    a, b, ca, cb = servers
    upload(cb, "ips", export(ca, "ips"))
    upload(cb, "domains", export(ca, "domains"))
    text = "# furnace-export whitelist-ips v1\ncidr,added_at\n198.51.100.0/30,%s\n" % db.to_iso(NOW)
    dry = upload(cb, "whitelist-ips", text, dry_run="true").get_json()
    assert dry["removed_from_blocklist"] == ["198.51.100.1", "198.51.100.2"]
    assert len(snapshot(b)["blocked_ips"]) == 4
    d = upload(cb, "whitelist-ips", text).get_json()
    assert d["added"] == 1 and d["removed_from_blocklist"] == ["198.51.100.1", "198.51.100.2"]
    assert "198.51.100.1" not in b.blocklist_path.read_text()
    shared = "# furnace-export whitelist-domains v1\ndomain,added_at\ngithub.io,%s\n" % db.to_iso(NOW)
    d = upload(cb, "whitelist-domains", shared).get_json()
    assert d["added"] == 0 and d["skipped_lines"][0]["reason_code"] == "whitelist_not_allowed"


def test_dry_run_changes_nothing(servers):
    _, b, ca, cb = servers
    for kind in ORDER:
        d = upload(cb, kind, export(ca, kind), dry_run="true").get_json()
        assert d["dry_run"] is True
    assert all(not rows for rows in snapshot(b).values())


def test_json_entries_upload(servers):
    _, b, ca, cb = servers
    lines = export(ca, "urls").splitlines()
    r = cb.post("/api/v1/urls/bulk", json={"entries": lines}, headers=ALL)
    assert r.get_json()["added"] == 2
    r = cb.post("/api/v1/whitelist/domains/bulk", json={"entries": export(ca, "whitelist-domains").splitlines()},
                headers=ALL)
    assert r.get_json()["furnace_export"] == "whitelist-domains" and r.get_json()["added"] == 1


def test_command_line(servers, tmp_path, monkeypatch, capsys):
    """furnace export writes the same file; furnace import-* reads it back."""
    from furnace import admin
    a, b, _, _ = servers
    for key, value in {"FURNACE_DATA_DIR": a.data_dir, "FURNACE_EDL_DIR": a.edl_dir,
                       "FURNACE_ARCHIVE_DIR": a.archive_dir, "FURNACE_BACKUP_DIR": a.backup_dir,
                       "FURNACE_AUTH_DIR": a.auth_dir, "FURNACE_CONFIG": tmp_path / "none.env",
                       "FURNACE_ALLOW_TEST_RANGES": "true", "FURNACE_ARCHIVE_AFTER_DAYS": "30"}.items():
        monkeypatch.setenv(key, str(value))
    out = tmp_path / "wl.csv"
    assert admin.main(["export", "whitelist-domains", "-o", str(out)]) == 0
    assert "sudo furnace import-whitelist-domains" in capsys.readouterr().out
    assert out.read_text().startswith("# furnace-export whitelist-domains v1\n")
    report = import_whitelist(b, out, "domains", report_path=tmp_path / "r.txt")
    assert report.furnace_file and report.added_active == 1
    assert "Furnace export (whitelist-domains)" in report.summary()
    with pytest.raises(ImportError_):
        import_domains(b, out, report_path=tmp_path / "r2.txt")
    assert json.loads(json.dumps(snapshot(b)["whitelist_domains"])) == \
        json.loads(json.dumps(snapshot(a)["whitelist_domains"]))
