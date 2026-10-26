import multiprocessing
from concurrent.futures import ThreadPoolExecutor

import pytest

from furnace.api import create_app
from furnace.config import ConfigError, Settings
from furnace.service import Furnace

from conftest import BLOCK_TOKEN, WHITELIST_TOKEN, leftover_temp_files

BLOCK = {"Authorization": f"Bearer {BLOCK_TOKEN}"}
WL = {"Authorization": f"Bearer {WHITELIST_TOKEN}"}


@pytest.fixture
def client(settings):
    return create_app(settings).test_client()


@pytest.fixture
def split_client(settings):
    settings.whitelist_token = WHITELIST_TOKEN
    return create_app(settings).test_client()


def test_refuses_to_start_without_block_token(settings):
    settings.block_token = ""
    with pytest.raises(ConfigError):
        create_app(settings)
    settings.block_token = "too-short"
    with pytest.raises(ConfigError):
        create_app(settings)


def test_health_needs_no_token(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.get_json()["status"] == "ok"
    assert r.get_json()["role"] == "leader"


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong-token-wrong-token-1234"},
                                     {"Authorization": BLOCK_TOKEN}, {"Authorization": "Basic abc"}])
def test_bad_tokens(client, headers):
    r = client.post("/api/v1/ips", json={"ip": "198.51.100.1"}, headers=headers)
    assert r.status_code == 401
    assert r.get_json()["status"] == "unauthorized"
    assert r.headers["WWW-Authenticate"] == "Bearer"


def test_add_ip_flow(client, settings):
    payload = {"ip": "198.51.100.20", "reason": "Malware C2", "added_by": "analyst.one", "incident_id": "4521"}
    r = client.post("/api/v1/ips", json=payload, headers=BLOCK)
    assert r.status_code == 201
    j = r.get_json()
    assert j["status"] == "added" and j["entry"]["incident_id"] == "4521"
    assert settings.blocklist_path.read_text() == "198.51.100.20\n"

    r = client.post("/api/v1/ips", json=payload, headers=BLOCK)
    assert r.status_code == 200 and r.get_json()["status"] == "already_exists"

    r = client.get("/api/v1/ips/198.51.100.20", headers=BLOCK)
    assert r.status_code == 200 and r.get_json()["list"] == "active"

    r = client.delete("/api/v1/ips/198.51.100.20?removed_by=analyst.two", headers=BLOCK)
    assert r.status_code == 200 and r.get_json()["status"] == "removed"
    assert client.delete("/api/v1/ips/198.51.100.20", headers=BLOCK).status_code == 404
    assert client.get("/api/v1/ips/198.51.100.20", headers=BLOCK).status_code == 404


def test_rejections_are_422_with_code(client):
    for ip, code in (("10.1.1.1", "private_ip"), ("198.51.100.0/24", "range_not_allowed"), ("x", "invalid_ip"),
                     (None, "missing_value")):
        r = client.post("/api/v1/ips", json={"ip": ip}, headers=BLOCK)
        assert r.status_code == 422
        assert r.get_json()["reason_code"] == code
        assert r.get_json()["message"]
    r = client.post("/api/v1/ips", json={"ip": "198.51.100.1", "reason": "x" * 501}, headers=BLOCK)
    assert r.get_json()["reason_code"] == "field_too_long"


def test_bad_body_is_400(client):
    r = client.post("/api/v1/ips", data="not json", headers={**BLOCK, "Content-Type": "application/json"})
    assert r.status_code == 400 and r.get_json()["status"] == "bad_request"
    r = client.post("/api/v1/ips", json=["198.51.100.1"], headers=BLOCK)
    assert r.status_code == 400


def test_unknown_route_and_method(client):
    assert client.get("/api/v1/nothing", headers=BLOCK).status_code == 404
    r = client.put("/api/v1/ips", headers=BLOCK)
    assert r.status_code == 405 and r.get_json()["status"] == "method_not_allowed"


def test_internal_errors_are_hidden(client, monkeypatch, settings):
    def explode(*a, **k):
        raise RuntimeError("secret internal detail /var/lib/furnace")
    monkeypatch.setattr(Furnace, "add_ip", explode)
    r = client.post("/api/v1/ips", json={"ip": "198.51.100.1"}, headers=BLOCK)
    assert r.status_code == 500
    assert "secret" not in r.get_data(as_text=True)
    assert "/var/lib" not in r.get_data(as_text=True)


def test_domains_api(client, settings):
    r = client.post("/api/v1/domains", json={"domain": "Bad.Example.com", "added_by": "a"}, headers=BLOCK)
    assert r.status_code == 201
    assert settings.domains_path.read_text() == "bad.example.com\n"
    assert client.post("/api/v1/domains", json={"domain": "bad.example.com"}, headers=BLOCK).status_code == 200
    r = client.post("/api/v1/domains", json={"domain": "*.example.com"}, headers=BLOCK)
    assert r.status_code == 422 and r.get_json()["reason_code"] == "wildcard_not_allowed"
    assert client.get("/api/v1/domains/bad.example.com", headers=BLOCK).status_code == 200
    assert client.delete("/api/v1/domains/bad.example.com", headers=BLOCK).status_code == 200
    assert client.delete("/api/v1/domains/bad.example.com", headers=BLOCK).status_code == 404


def test_shared_token_when_no_whitelist_token(client):
    r = client.post("/api/v1/whitelist/ips", json={"entry": "198.51.100.0/24"}, headers=BLOCK)
    assert r.status_code == 201


def test_separate_whitelist_token(split_client):
    c = split_client
    assert c.post("/api/v1/whitelist/ips", json={"entry": "198.51.100.0/24"}, headers=BLOCK).status_code == 401
    assert c.post("/api/v1/ips", json={"ip": "203.0.113.1"}, headers=WL).status_code == 401
    assert c.post("/api/v1/whitelist/ips", json={"entry": "198.51.100.0/24"}, headers=WL).status_code == 201
    assert c.post("/api/v1/ips", json={"ip": "203.0.113.1"}, headers=BLOCK).status_code == 201


def test_whitelist_api(split_client, settings):
    c = split_client
    c.post("/api/v1/ips", json={"ip": "203.0.113.5"}, headers=BLOCK)
    r = c.post("/api/v1/whitelist/ips", json={"entry": "203.0.113.0/24", "reason": "partner"}, headers=WL)
    assert r.status_code == 201 and r.get_json()["removed_from_blocklist"] == ["203.0.113.5"]
    assert settings.blocklist_path.read_text() == ""
    r = c.post("/api/v1/ips", json={"ip": "203.0.113.9"}, headers=BLOCK)
    assert r.status_code == 422 and r.get_json()["reason_code"] == "whitelisted"
    assert c.post("/api/v1/whitelist/ips", json={"entry": "0.0.0.0/0"}, headers=WL).status_code == 422
    r = c.get("/api/v1/whitelist/ips?limit=10", headers=WL)
    assert r.get_json()["total"] == 1
    assert c.delete("/api/v1/whitelist/ips/203.0.113.0/24", headers=WL).status_code == 200
    assert c.delete("/api/v1/whitelist/ips/203.0.113.0%2F24", headers=WL).status_code == 404

    assert c.post("/api/v1/whitelist/domains", json={"domain": "good.example.com"}, headers=WL).status_code == 201
    assert c.get("/api/v1/whitelist/domains", headers=WL).get_json()["total"] == 1
    assert c.delete("/api/v1/whitelist/domains/good.example.com", headers=WL).status_code == 200


def test_bulk_json_and_text(split_client):
    c = split_client
    r = c.post("/api/v1/whitelist/ips/bulk", json={"entries": ["198.51.100.0/24", "bad"], "dry_run": True},
               headers=WL)
    assert r.status_code == 200
    j = r.get_json()
    assert j["dry_run"] is True and j["added"] == 1 and j["rejected"][0]["reason_code"] == "invalid_cidr"
    assert c.get("/api/v1/whitelist/ips", headers=WL).get_json()["total"] == 0

    text = "# partner ranges\n198.51.100.0/24\n\n203.0.113.0/25\n"
    r = c.post("/api/v1/whitelist/ips/bulk?added_by=admin", data=text,
               headers={**WL, "Content-Type": "text/plain"})
    assert r.get_json()["added"] == 2
    r = c.post("/api/v1/whitelist/domains/bulk", json={"entries": ["a.example.com", "b.example.com"]}, headers=WL)
    assert r.get_json()["added"] == 2
    r = c.post("/api/v1/whitelist/domains/bulk", json={"entries": "a.example.com"}, headers=WL)
    assert r.status_code == 400
    r = c.post("/api/v1/whitelist/domains/bulk", json={"entries": [], "dry_run": "yes"}, headers=WL)
    assert r.status_code == 400


def test_follower_answers_503(settings, tmp_path):
    role = tmp_path / "role"
    role.write_text("follower")
    settings.role_file = role
    c = create_app(settings).test_client()
    r = c.post("/api/v1/ips", json={"ip": "198.51.100.1"}, headers=BLOCK)
    assert r.status_code == 503 and r.get_json()["reason_code"] == "not_leader"
    assert c.get("/health").get_json()["role"] == "follower"


def test_many_requests_at_the_same_time(client, settings):
    ips = [f"198.51.100.{i}" for i in range(1, 101)] * 2  # every IP twice, at once

    def call(ip):
        return client.post("/api/v1/ips", json={"ip": ip}, headers=BLOCK).status_code

    with ThreadPoolExecutor(max_workers=16) as pool:
        codes = list(pool.map(call, ips))
    assert codes.count(201) == 100
    assert codes.count(200) == 100
    lines = settings.blocklist_path.read_text().splitlines()
    assert len(lines) == 100 and len(set(lines)) == 100
    assert leftover_temp_files(settings.edl_dir) == []


def _worker(args):
    settings, start = args
    f = Furnace(settings)
    return [f.add_ip(f"203.0.113.{i}").status for i in range(start, start + 25)]


def test_many_processes_at_the_same_time(settings):
    Furnace(settings).startup()
    ctx = multiprocessing.get_context("fork")
    with ctx.Pool(4) as pool:
        # two processes per block of IPs, so they race on the same entries
        results = pool.map(_worker, [(settings, 1), (settings, 1), (settings, 26), (settings, 26)])
    flat = [s for part in results for s in part]
    assert flat.count("added") == 50 and flat.count("already_exists") == 50
    assert len(settings.blocklist_path.read_text().splitlines()) == 50
    assert leftover_temp_files(settings.edl_dir) == []
