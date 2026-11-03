"""GET /api/v1/whitelist/check/<ip-or-domain> (user decision 2026-11-03): is this on the whitelist?
It needs only the whitelist read permission, also answers for private IPs, and names the entry
that protects the value (a range or a parent domain)."""

from __future__ import annotations

import pytest

from furnace.api import create_app

from conftest import ALL_TOKEN, BLOCK_TOKEN, WHITELIST_TOKEN

BLOCK = {"Authorization": f"Bearer {BLOCK_TOKEN}"}
WHITE = {"Authorization": f"Bearer {WHITELIST_TOKEN}"}
ALL = {"Authorization": f"Bearer {ALL_TOKEN}"}


@pytest.fixture
def client(settings):
    c = create_app(settings).test_client()
    for entry in ("198.51.100.0/24", "10.0.0.0/8", "2001:db8::/32"):
        assert c.post("/api/v1/whitelist/ips", json={"entry": entry, "reason": "ours", "added_by": "lab"},
                      headers=WHITE).status_code == 201
    assert c.post("/api/v1/whitelist/domains", json={"domain": "good.example.com", "reason": "ours"},
                  headers=WHITE).status_code == 201
    return c


def check(client, value, headers=WHITE):
    r = client.get(f"/api/v1/whitelist/check/{value}", headers=headers)
    return r.status_code, r.get_json()


@pytest.mark.parametrize("value, shown, kind, by", [
    ("198.51.100.7", "198.51.100.7", "ip", "198.51.100.0/24"),
    ("10.1.2.3", "10.1.2.3", "ip", "10.0.0.0/8"),                      # private IPs too
    ("2001:db8::5", "2001:db8::5", "ip", "2001:db8::/32"),
    ("::ffff:198.51.100.7", "198.51.100.7", "ip", "198.51.100.0/24"),  # IPv6-mapped form
    ("good.example.com", "good.example.com", "domain", "good.example.com"),
    ("WWW.Good.Example.COM.", "www.good.example.com", "domain", "good.example.com"),  # a subdomain
])
def test_whitelisted(client, value, shown, kind, by):
    code, d = check(client, value)
    assert code == 200
    assert (d["value"], d["type"], d["whitelisted"], d["whitelisted_by"]) == (shown, kind, True, by)
    assert d["entry"]["reason"] == "ours"


def test_messages_name_the_entry(client):
    assert check(client, "good.example.com")[1]["message"] == "good.example.com is on the whitelist."
    assert check(client, "a.good.example.com")[1]["message"] == \
        "a.good.example.com is on the whitelist through the parent domain good.example.com."
    assert check(client, "10.9.9.9")[1]["message"] == "10.9.9.9 is on the whitelist through the range 10.0.0.0/8."


@pytest.mark.parametrize("value, kind", [
    ("203.0.113.9", "ip"),
    ("192.168.1.1", "ip"),
    ("example.com", "domain"),           # the parent of a whitelisted domain is not protected
    ("bad.example.com", "domain"),
    ("xgood.example.com", "domain"),
])
def test_not_whitelisted(client, value, kind):
    code, d = check(client, value)
    assert code == 200
    assert (d["type"], d["whitelisted"], d["whitelisted_by"], d["entry"]) == (kind, False, None, None)
    assert d["message"].endswith("is not on the whitelist.")


@pytest.mark.parametrize("value, reason", [
    ("*.example.com", "wildcard_not_allowed"),
    ("bad_name", "invalid_domain"),
    ("localhost", "invalid_domain"),
])
def test_bad_values(client, value, reason):
    code, d = check(client, value)
    assert (code, d["reason_code"]) == (422, reason)
    assert "Send one IP address or one domain name." in d["message"]


def test_permissions(client):
    assert check(client, "10.1.2.3", headers=ALL)[0] == 200
    assert check(client, "10.1.2.3", headers=BLOCK)[0] == 401      # blocklist only: no whitelist read
    assert check(client, "10.1.2.3", headers={})[0] == 401


def test_works_on_the_follower(client, settings, tmp_path):
    """A read: the standby answers too, like the other lookups."""
    role = tmp_path / "role"
    role.write_text("follower")
    settings.role_file = role
    c = create_app(settings).test_client()
    assert c.get("/health").get_json()["role"] == "follower"
    code, d = check(c, "10.1.2.3")
    assert (code, d["whitelisted_by"]) == (200, "10.0.0.0/8")
