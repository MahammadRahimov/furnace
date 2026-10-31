"""Bulk upload of blocked IPs and domains through the API (the same rules as 'furnace import-ips')."""

from datetime import timedelta

import pytest

from furnace import api, db
from furnace.api import create_app

from conftest import BLOCK_TOKEN, WHITELIST_TOKEN

BLOCK = {"Authorization": f"Bearer {BLOCK_TOKEN}"}


@pytest.fixture
def client(settings):
    return create_app(settings).test_client()


def upload(client, path, text, query="", headers=BLOCK):
    return client.post(f"{path}{query}", data=text, headers=headers, content_type="text/plain")


def blocked(settings) -> list[str]:
    return settings.blocklist_path.read_text().split()


def test_a_file_of_ips(client, settings):
    recent = (db.utcnow() - timedelta(days=3)).strftime("%Y-%m-%d")
    text = (f"ip,date,reason\n198.51.100.1,{recent},phishing\n198.51.100.2\n198.51.100.2\n"
            "10.0.0.1\nnot-an-ip\n# a comment\n\n203.0.113.5;;scanner\n")
    r = upload(client, "/api/v1/ips/bulk", text, "?dry_run=true")
    assert r.status_code == 200, r.get_json()
    a = r.get_json()
    assert (a["dry_run"], a["received"], a["added"], a["skipped"]) == (True, 7, 3, 4)
    assert {s["reason_code"] for s in a["skipped_lines"]} == {"header", "duplicate_in_file", "private_ip",
                                                              "invalid_ip"}
    assert blocked(settings) == []  # a dry run changes nothing

    a = upload(client, "/api/v1/ips/bulk", text, "?added_by=soar").get_json()
    assert (a["dry_run"], a["added"], a["permanent"]) == (False, 3, False)
    assert sorted(blocked(settings)) == ["198.51.100.1", "198.51.100.2", "203.0.113.5"]
    conn = db.connect(settings.db_path)
    try:
        row = conn.execute("SELECT added_by, reason FROM blocked_ips WHERE ip='198.51.100.1'").fetchone()
        assert tuple(row) == ("soar", "phishing")
        assert conn.execute("SELECT reason FROM blocked_ips WHERE ip='198.51.100.2'").fetchone()[0] \
            == "uploaded through the API"
    finally:
        conn.close()
    assert list((settings.data_dir / "uploads").iterdir()) == []  # the upload file is deleted
    assert (settings.data_dir / "reports" / a["report"]).is_file()  # the report stays on the server

    again = upload(client, "/api/v1/ips/bulk", "198.51.100.1\n").get_json()
    assert (again["added"], again["skipped_lines"][0]["reason_code"]) == (0, "already_active")


def test_permanent_old_and_future_dates(client, settings):
    old = (db.utcnow() - timedelta(days=90)).strftime("%Y-%m-%d")
    future = (db.utcnow() + timedelta(days=9)).strftime("%Y-%m-%d")
    a = upload(client, "/api/v1/ips/bulk", f"198.51.100.10,{old}\n198.51.100.11,{future}\n").get_json()
    assert (a["added"], a["archived"]) == (0, 1)
    assert a["skipped_lines"][0]["reason_code"] == "future_date"
    a = upload(client, "/api/v1/ips/bulk", f"198.51.100.12,{old}\n", "?permanent=true").get_json()
    assert (a["added"], a["permanent"]) == (1, True)
    conn = db.connect(settings.db_path)
    try:
        assert conn.execute("SELECT permanent FROM blocked_ips WHERE ip='198.51.100.12'").fetchone()[0] == 1
    finally:
        conn.close()


def test_domains_as_json(client, settings):
    r = client.post("/api/v1/domains/bulk", headers=BLOCK,
                    json={"entries": ["bad.example.com", "BAD.example.com", "worse.example.net,malware",
                                      "http://x"], "reason": "feed"})
    a = r.get_json()
    assert r.status_code == 200, a
    assert (a["added"], a["skipped"]) == (2, 2)
    assert "permanent" not in a
    assert settings.domains_path.read_text().split() == ["bad.example.com", "worse.example.net"]


def test_whitelisted_entries_are_skipped(client, settings):
    client.post("/api/v1/whitelist/ips", json={"entry": "198.51.100.0/28"},
                headers={"Authorization": f"Bearer {WHITELIST_TOKEN}"})
    a = upload(client, "/api/v1/ips/bulk", "198.51.100.3\n198.51.100.30\n").get_json()
    assert a["added"] == 1 and a["skipped_lines"][0]["reason_code"] == "whitelisted"


def test_it_needs_the_blocklist_write_permission(client):
    r = upload(client, "/api/v1/ips/bulk", "198.51.100.1\n", headers={"Authorization": f"Bearer {WHITELIST_TOKEN}"})
    assert r.status_code == 401
    assert upload(client, "/api/v1/domains/bulk", "bad.example.com\n", headers={}).status_code == 401


def test_bad_requests(client, monkeypatch):
    assert upload(client, "/api/v1/ips/bulk", "\n\n").status_code == 400
    assert upload(client, "/api/v1/ips/bulk", "198.51.100.1\n", "?permanent=maybe").status_code == 400
    assert upload(client, "/api/v1/ips/bulk", "198.51.100.1\n", "?old_ips=keep").status_code == 400
    assert client.post("/api/v1/ips/bulk", headers=BLOCK, json={"entries": "198.51.100.1"}).status_code == 400
    monkeypatch.setattr(api, "MAX_BULK_ENTRIES", 2)
    r = upload(client, "/api/v1/ips/bulk", "198.51.100.1\n198.51.100.2\n198.51.100.3\n")
    assert r.status_code == 400 and "at most 2 lines" in r.get_json()["message"]


def test_the_list_limit_stops_the_whole_upload(settings):
    settings.max_ips = 2
    c = create_app(settings).test_client()
    r = upload(c, "/api/v1/ips/bulk", "198.51.100.1\n198.51.100.2\n198.51.100.3\n")
    assert r.status_code == 422 and "limit is 2" in r.get_json()["message"]
    assert blocked(settings) == []  # all or nothing


def test_the_follower_refuses(settings, tmp_path):
    role = tmp_path / "role"
    role.write_text("follower\n")
    settings.role_file = role
    r = upload(create_app(settings).test_client(), "/api/v1/ips/bulk", "198.51.100.1\n")
    assert r.status_code == 503 and r.get_json()["status"] == "not_leader"
