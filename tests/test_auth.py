"""API tokens, the wrong-token lockout, EDL logins and the token migration from 0.2."""

import os
import sqlite3
import subprocess

import pytest

from furnace import admin, auth, backup, db, edl
from furnace.api import create_app
from furnace.config import Settings
from furnace.replica import apply_snapshot
from furnace.service import Furnace

from conftest import ALL_TOKEN, BLOCK_TOKEN

BLOCK = {"Authorization": f"Bearer {BLOCK_TOKEN}"}


@pytest.fixture
def client(settings):
    return create_app(settings).test_client()


def wrong(n):
    return {"Authorization": f"Bearer wrong-token-number-{n:04d}"}


# ---------------------------------------------------------------------- tokens

def test_only_the_hash_is_stored(conn):
    token = auth.add_token(conn, "Robot.One", "block", "tester")
    assert token.startswith("fur_") and len(token) > 40
    dump = "\n".join(conn.iterdump())
    assert token not in dump
    assert auth.hash_token(token) in dump
    assert [t["name"] for t in auth.list_tokens(conn)] == ["block", "robot.one", "ui", "whitelist"]


def test_token_names_and_scopes_are_checked(conn):
    for name in ("", "-x", "a b", "x" * 41, "ünï"):
        with pytest.raises(auth.AuthError):
            auth.add_token(conn, name, "block", "tester")
    with pytest.raises(auth.AuthError):
        auth.add_token(conn, "robot", "admin", "tester")
    with pytest.raises(auth.AuthError, match="exists already"):
        auth.add_token(conn, "block", "block", "tester")
    with pytest.raises(auth.AuthError, match="no token"):
        auth.rotate_token(conn, "nobody", "tester")
    with pytest.raises(auth.AuthError, match="no token"):
        auth.remove_token(conn, "nobody", "tester")


def test_rotate_and_remove_work_at_once(client, conn):
    assert client.get("/api/v1/ips/198.51.100.1", headers=BLOCK).status_code == 404
    new = auth.rotate_token(conn, "block", "tester")
    assert client.get("/api/v1/ips/198.51.100.1", headers=BLOCK).status_code == 401
    fresh = {"Authorization": f"Bearer {new}"}
    assert client.get("/api/v1/ips/198.51.100.1", headers=fresh).status_code == 404
    auth.remove_token(conn, "block", "tester")
    assert client.get("/api/v1/ips/198.51.100.1", headers=fresh).status_code == 401
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_log WHERE object_type='api_token'")]
    assert actions == ["token_add", "token_add", "token_add", "token_rotate", "token_remove"]


def test_the_audit_log_names_the_token(client, conn):
    client.post("/api/v1/ips", json={"ip": "198.51.100.5"}, headers=BLOCK)
    source = conn.execute("SELECT source FROM audit_log WHERE action='add'").fetchone()[0]
    assert source.startswith("api:block@")


def test_selftest_token_only_with_a_valid_flag(settings, tmp_path, monkeypatch):
    flag = tmp_path / "selftest"
    flag.write_text("setup-selftest-token-0123456789")
    settings.selftest_flag = flag
    c = create_app(settings).test_client()
    header = {"Authorization": "Bearer setup-selftest-token-0123456789"}
    if os.getuid() != 0:  # a flag not owned by root never counts
        assert c.get("/api/v1/ips/198.51.100.1", headers=header).status_code == 401
    monkeypatch.setattr(Settings, "_selftest_flag_valid", lambda self: True)
    assert c.get("/api/v1/ips/198.51.100.1", headers=header).status_code == 404
    flag.write_text("short")
    assert c.get("/api/v1/ips/198.51.100.1", headers={"Authorization": "Bearer short"}).status_code == 401


# ---------------------------------------------------------------------- lockout

def test_lockout_after_too_many_wrong_tokens(client, settings):
    settings.lockout_failures = 3
    for n in range(2):
        assert client.get("/api/v1/ips/198.51.100.1", headers=wrong(n)).status_code == 401
    assert client.get("/api/v1/ips/198.51.100.1", headers=BLOCK).status_code == 404  # still allowed
    assert client.get("/api/v1/ips/198.51.100.1", headers=wrong(3)).status_code == 401  # third: locked now
    r = client.get("/api/v1/ips/198.51.100.1", headers=BLOCK)
    assert r.status_code == 429 and r.get_json()["status"] == "locked"
    assert 0 < int(r.headers["Retry-After"]) <= 15 * 60 + 1
    assert [x["ip"] for x in auth.list_lockouts(settings)] == ["127.0.0.1"]
    assert auth.unlock(settings, "127.0.0.1") == 1
    assert client.get("/api/v1/ips/198.51.100.1", headers=BLOCK).status_code == 404


def test_missing_token_and_wrong_scope_do_not_count(client, settings):
    settings.lockout_failures = 2
    for _ in range(5):
        assert client.get("/api/v1/ips/198.51.100.1").status_code == 401
        assert client.get("/api/v1/whitelist/ips", headers=BLOCK).status_code == 401
    assert client.get("/api/v1/ips/198.51.100.1", headers=BLOCK).status_code == 404


def test_lockout_window_and_expiry(settings):
    settings.lockout_failures = 2
    t = 1_000_000.0
    assert not auth.record_failure(settings, "192.0.2.9", now=t)
    assert not auth.record_failure(settings, "192.0.2.9", now=t + 16 * 60)  # the first one is too old
    assert auth.record_failure(settings, "192.0.2.9", now=t + 17 * 60)
    assert auth.locked_for(settings, "192.0.2.9", now=t + 17 * 60) > 0
    assert auth.locked_for(settings, "192.0.2.8", now=t + 17 * 60) == 0
    assert auth.locked_for(settings, "192.0.2.9", now=t + 33 * 60) == 0


# ---------------------------------------------------------------------- EDL logins

def openssl_check(password: str, hashed: str) -> bool:
    salt = hashed.split("$")[2]
    out = subprocess.run(["openssl", "passwd", "-6", "-salt", salt, "-stdin"], input=password + "\n",
                         capture_output=True, text=True, check=True).stdout.strip()
    return out == hashed


def test_edl_users_file(conn, settings):
    edl.ensure_files(settings)
    assert settings.edl_users_path.read_text() == ""
    password = auth.add_edl_user(conn, "Lists", "tester")
    assert len(password) >= 32
    edl.rebuild_all(conn, settings)
    line = settings.edl_users_path.read_text()
    name, hashed = line.strip().split(":", 1)
    assert name == "lists" and hashed.startswith("$6$") and password not in line
    assert openssl_check(password, hashed)
    assert oct(settings.edl_users_path.stat().st_mode & 0o777) == "0o640"

    new = auth.rotate_edl_user(conn, "lists", "tester")
    edl.write_edl_users(conn, settings)
    hashed2 = settings.edl_users_path.read_text().strip().split(":", 1)[1]
    assert openssl_check(new, hashed2) and not openssl_check(password, hashed2)
    with pytest.raises(auth.AuthError):
        auth.add_edl_user(conn, "lists", "tester")
    auth.remove_edl_user(conn, "lists", "tester")
    edl.write_edl_users(conn, settings)
    assert settings.edl_users_path.read_text() == ""


# ---------------------------------------------------------------------- upgrade from 0.2

def test_legacy_tokens_keep_working(settings, conn):
    conn.execute("DELETE FROM api_tokens")
    old = {"FURNACE_BLOCK_TOKEN": "old-block-token-0123456789", "FURNACE_WHITELIST_TOKEN": "old-wl-token-0123456789"}
    assert auth.import_legacy_tokens(conn, old) == ["block", "whitelist"]
    assert auth.import_legacy_tokens(conn, old) == []  # only once
    c = create_app(settings).test_client()
    assert c.get("/api/v1/ips/198.51.100.1",
                 headers={"Authorization": "Bearer old-block-token-0123456789"}).status_code == 404
    assert c.get("/api/v1/whitelist/ips",
                 headers={"Authorization": "Bearer old-wl-token-0123456789"}).status_code == 200

    conn.execute("DELETE FROM api_tokens")
    assert auth.import_legacy_tokens(conn, {"FURNACE_BLOCK_TOKEN": "old-block-token-0123456789"}) == ["block"]
    assert auth.list_tokens(conn)[0]["permissions"] == auth.PERMISSIONS  # 0.2: one token did both


def test_snapshot_from_an_older_leader(settings, tmp_path):
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as c:
        c.executescript(db.SCHEMA.split("-- API tokens.")[0])
        c.execute("INSERT INTO blocked_ips VALUES ('198.51.100.3', 4, 't', 't', '', '', '')")
    follower = Settings(data_dir=tmp_path / "f", edl_dir=tmp_path / "f/edl", archive_dir=tmp_path / "f/arch",
                        backup_dir=tmp_path / "f/bk", auth_dir=tmp_path / "f/auth", role_file=tmp_path / "role")
    (tmp_path / "role").write_text("follower")
    Furnace(follower).startup()
    assert apply_snapshot(follower, old)["ips"] == 1
    c = create_app(follower).test_client()
    assert c.get("/api/v1/ips/198.51.100.3", headers=BLOCK).status_code == 401  # no tokens yet, no crash


def test_tokens_and_logins_replicate(settings, conn, tmp_path):
    auth.add_edl_user(conn, "lists", "tester")
    snap = backup.snapshot(settings, tmp_path / "snap.db")
    follower = Settings(data_dir=tmp_path / "f", edl_dir=tmp_path / "f/edl", archive_dir=tmp_path / "f/arch",
                        backup_dir=tmp_path / "f/bk", auth_dir=tmp_path / "f/auth", role_file=tmp_path / "role")
    (tmp_path / "role").write_text("follower")
    Furnace(follower).startup()
    apply_snapshot(follower, snap)
    assert follower.edl_users_path.read_text().startswith("lists:$6$")
    r = create_app(follower).test_client().post("/api/v1/ips", json={"ip": "198.51.100.1"}, headers=BLOCK)
    assert r.status_code == 503  # the token is known; the follower refuses the change


# ---------------------------------------------------------------------- the furnace command

@pytest.fixture
def cli(settings, tmp_path, monkeypatch):
    env = {"FURNACE_DATA_DIR": str(settings.data_dir), "FURNACE_EDL_DIR": str(settings.edl_dir),
           "FURNACE_ARCHIVE_DIR": str(settings.archive_dir), "FURNACE_BACKUP_DIR": str(settings.backup_dir),
           "FURNACE_AUTH_DIR": str(settings.auth_dir), "FURNACE_ACTOR": "mahammad"}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("FURNACE_CONFIG", str(tmp_path / "none.env"))
    return admin.main


def test_cli_tokens(cli, capsys, settings, tmp_path, monkeypatch):
    assert cli(["token", "add", "ui-app"]) == 2  # no terminal: the options are needed
    assert "Give --allow and --expires-days" in capsys.readouterr().err
    assert cli(["token", "add", "ui-app", "--allow", "blocklist:write,whitelist:read", "--expires-days", "30"]) == 0
    out = capsys.readouterr().out
    assert "blocklist read+write, whitelist read" in out and "(in 30 days)" in out
    token = out.split("Store it now:")[1].strip()
    assert token.startswith("fur_")
    assert cli(["token", "list"]) == 0
    listing = capsys.readouterr().out
    assert "ui-app" in listing and token not in listing and BLOCK_TOKEN not in listing
    row = next(line for line in listing.splitlines() if line.startswith("ui-app "))
    assert row.split()[1:3] == ["read+write", "read"] and "(in 30 days)" in row and "never" in row
    assert cli(["token", "edit", "ui-app", "--allow", "whitelist:write"]) == 0
    assert "blocklist -, whitelist read+write" in capsys.readouterr().out
    assert cli(["token", "edit", "ui-app"]) == 2  # nothing to change
    assert cli(["token", "rotate", "ui-app"]) == 0
    out = capsys.readouterr().out
    assert token not in out and "(in 30 days)" in out  # the same number of days again
    assert cli(["token", "rotate", "ui-app", "--expires-days", "0"]) == 0
    assert "Expires: never" in capsys.readouterr().out
    assert cli(["token", "remove", "ui-app"]) == 0
    assert cli(["token", "remove", "ui-app"]) == 2
    conn = db.connect(settings.db_path)
    assert conn.execute("SELECT actor FROM audit_log WHERE action='token_remove'").fetchone()[0] == "mahammad"
    conn.close()

    (tmp_path / "role").write_text("follower")
    monkeypatch.setenv("FURNACE_ROLE_FILE", str(tmp_path / "role"))
    assert cli(["token", "add", "late", "--allow", "all", "--expires-days", "0"]) == 2
    assert "Run this on the leader" in capsys.readouterr().err
    assert cli(["token", "list"]) == 0  # reading is fine on the follower


def test_cli_edl_users_and_lockouts(cli, capsys, settings, monkeypatch):
    assert cli(["edl-user", "list"]) == 0
    assert "nobody can download" in capsys.readouterr().out
    assert cli(["edl-user", "add", "lists"]) == 0
    password = capsys.readouterr().out.split("Store it now:")[1].strip()
    hashed = settings.edl_users_path.read_text().strip().split(":", 1)[1]
    assert openssl_check(password, hashed)
    assert cli(["stats"]) == 0
    stats = capsys.readouterr().out
    assert "api tokens" in stats and "edl logins" in stats

    monkeypatch.setenv("FURNACE_LOCKOUT_FAILURES", "1")
    assert auth.record_failure(Settings.load(), "192.0.2.44")
    assert cli(["lockouts"]) == 0
    assert "192.0.2.44" in capsys.readouterr().out
    assert cli(["unlock"]) == 2
    assert cli(["unlock", "--all"]) == 0
    assert cli(["lockouts"]) == 0
    assert "No address is locked" in capsys.readouterr().out


def test_cli_init_secrets(cli, capsys, settings, tmp_path):
    conn = db.connect(settings.db_path)
    conn.execute("DELETE FROM api_tokens")
    conn.close()
    legacy = tmp_path / "tokens.env"
    legacy.write_text("FURNACE_BLOCK_TOKEN=old-block-token-0123456789\nFURNACE_WHITELIST_TOKEN=\n")
    assert cli(["init-secrets", "--legacy-tokens", str(legacy)]) == 0
    lines = [line.split("\t") for line in capsys.readouterr().out.splitlines()]
    assert lines[0] == ["migrated", "block"]
    assert lines[1][:2] == ["edl", "lists"] and len(lines) == 2
    assert cli(["init-secrets"]) == 0
    assert capsys.readouterr().out == ""  # nothing new the second time

    conn = db.connect(settings.db_path)
    conn.execute("DELETE FROM api_tokens")
    conn.close()
    assert cli(["init-secrets"]) == 0
    made = [line.split("\t") for line in capsys.readouterr().out.splitlines()]
    assert [(m[0], m[1]) for m in made] == [("token", "api")]  # one token, all permissions, no expiry
    assert made[0][2] == "Permissions: blocklist read+write, whitelist read+write. Expires: never."
    c = create_app(settings).test_client()
    assert c.get("/api/v1/ips/198.51.100.1", headers={"Authorization": f"Bearer {made[0][3]}"}).status_code == 404
    assert c.get("/api/v1/ips/198.51.100.1", headers={"Authorization": f"Bearer {ALL_TOKEN}"}).status_code == 401

    conn = db.connect(settings.db_path)
    conn.execute("DELETE FROM api_tokens")
    conn.close()
    assert cli(["init-secrets", "--token-name", "soar", "--allow", "blocklist:write", "--expires-days", "90"]) == 0
    made = [line.split("\t") for line in capsys.readouterr().out.splitlines()]
    assert made[0][1] == "soar"
    assert "blocklist read+write, whitelist -" in made[0][2] and "(in 90 days)" in made[0][2]


# ---------------------------------------------------------------------- permissions and expiry (0.6)

def headers(token):
    return {"Authorization": f"Bearer {token}"}


def test_permission_names(conn):
    assert auth.parse_permissions("all") == auth.PERMISSIONS
    assert auth.parse_permissions("block") == ("blocklist:read", "blocklist:write")
    assert auth.parse_permissions(" Whitelist:Write ") == ("whitelist:read", "whitelist:write")  # write brings read
    assert auth.parse_permissions(["blocklist:read", "whitelist:read"]) == ("blocklist:read", "whitelist:read")
    for bad in ("", "admin", "blocklist:delete", ","):
        with pytest.raises(auth.AuthError):
            auth.parse_permissions(bad)
    for bad in (-1, 3651, "x", "1.5", None):
        with pytest.raises(auth.AuthError, match="0 to 3650"):
            auth.add_token(conn, "robot", "all", "tester", expires_days=bad)


def test_read_and_write_permissions(client, conn):
    reader = auth.add_token(conn, "reader", "blocklist:read,whitelist:read", "tester")
    writer = auth.add_token(conn, "writer", "whitelist:write", "tester")
    assert client.get("/api/v1/ips/198.51.100.1", headers=headers(reader)).status_code == 404
    assert client.get("/api/v1/domains/bad.example.com", headers=headers(reader)).status_code == 404
    assert client.get("/api/v1/whitelist/ips", headers=headers(reader)).status_code == 200
    r = client.post("/api/v1/ips", json={"ip": "198.51.100.1"}, headers=headers(reader))
    assert r.status_code == 401 and "blocklist write permission" in r.get_json()["message"]
    assert client.delete("/api/v1/ips/198.51.100.1", headers=headers(reader)).status_code == 401
    assert client.post("/api/v1/whitelist/ips", json={"entry": "198.51.100.0/24"},
                       headers=headers(reader)).status_code == 401
    assert client.post("/api/v1/whitelist/ips", json={"entry": "198.51.100.0/24"},
                       headers=headers(writer)).status_code == 201
    assert client.get("/api/v1/whitelist/ips", headers=headers(writer)).status_code == 200  # write includes read
    assert client.get("/api/v1/ips/198.51.100.1", headers=headers(writer)).status_code == 401


def test_expired_token(client, conn, settings):
    settings.lockout_failures = 2
    token = auth.add_token(conn, "short", "all", "tester", expires_days=1)
    assert client.get("/api/v1/ips/198.51.100.1", headers=headers(token)).status_code == 404
    conn.execute("UPDATE api_tokens SET expires_at = '2026-01-02T00:00:00Z' WHERE name = 'short'")
    for _ in range(4):  # an expired token is a real token: no lockout
        r = client.get("/api/v1/ips/198.51.100.1", headers=headers(token))
        assert r.status_code == 401 and "expired on 2026-01-02" in r.get_json()["message"]
    assert client.get("/api/v1/ips/198.51.100.1", headers=BLOCK).status_code == 404
    warnings = admin.token_warnings(conn)
    assert len(warnings) == 1 and "'short' has expired" in warnings[0]
    auth.rotate_token(conn, "short", "tester")  # a new value, again for one day
    assert auth.get_token(conn, "short")["expires"].endswith("(in 1 day)")
    assert "'short' expires soon" in admin.token_warnings(conn)[0]


def test_last_used_is_kept_on_the_leader(client, conn, settings, tmp_path):
    assert auth.get_token(conn, "block")["last_used_at"] == ""
    client.get("/api/v1/ips/198.51.100.1", headers=BLOCK)
    first = auth.get_token(conn, "block")["last_used_at"]
    assert first
    client.get("/api/v1/ips/198.51.100.1", headers=BLOCK)
    assert auth.get_token(conn, "block")["last_used_at"] == first  # at most once an hour
    conn.execute("UPDATE api_tokens SET last_used_at = '' WHERE name = 'block'")
    (tmp_path / "role").write_text("follower")
    settings.role_file = tmp_path / "role"
    client.get("/api/v1/ips/198.51.100.1", headers=BLOCK)
    assert auth.get_token(conn, "block")["last_used_at"] == ""  # the follower writes nothing


OLD_TOKENS_TABLE = """CREATE TABLE api_tokens (name TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
    scope TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL DEFAULT '')"""


def test_tokens_of_version_0_5_keep_working(tmp_path):
    s = Settings(data_dir=tmp_path / "d", edl_dir=tmp_path / "e", archive_dir=tmp_path / "a",
                 backup_dir=tmp_path / "b", auth_dir=tmp_path / "au")
    s.ensure_dirs()
    with sqlite3.connect(s.db_path) as c:
        c.execute(OLD_TOKENS_TABLE)
        for name, scope in (("block", "block"), ("whitelist", "whitelist"), ("ui", "all")):
            c.execute("INSERT INTO api_tokens VALUES (?, ?, ?, 't', 'setup')",
                      (name, auth.hash_token(f"old-{name}-token-0123456789"), scope))
    client = create_app(s).test_client()  # the start-up upgrades the database
    old = {n: headers(f"old-{n}-token-0123456789") for n in ("block", "whitelist", "ui")}
    assert client.get("/api/v1/ips/198.51.100.1", headers=old["block"]).status_code == 404
    assert client.get("/api/v1/whitelist/ips", headers=old["block"]).status_code == 401
    assert client.get("/api/v1/whitelist/ips", headers=old["whitelist"]).status_code == 200
    assert client.post("/api/v1/whitelist/ips", json={"entry": "198.51.100.0/24"}, headers=old["ui"]).status_code == 201
    conn = db.connect(s.db_path)
    try:
        assert [t["expires"] for t in auth.list_tokens(conn)] == ["never"] * 3
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "3"
    finally:
        conn.close()


def test_snapshot_from_a_0_5_leader_with_tokens(settings, tmp_path):
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as c:
        c.executescript(db.SCHEMA.split("-- API tokens.")[0])
        c.execute(OLD_TOKENS_TABLE)
        c.execute("INSERT INTO api_tokens VALUES ('block', ?, 'block', 't', 'setup')", (auth.hash_token(BLOCK_TOKEN),))
    follower = Settings(data_dir=tmp_path / "f", edl_dir=tmp_path / "f/edl", archive_dir=tmp_path / "f/arch",
                        backup_dir=tmp_path / "f/bk", auth_dir=tmp_path / "f/auth", role_file=tmp_path / "role")
    (tmp_path / "role").write_text("follower")
    Furnace(follower).startup()
    apply_snapshot(follower, old)
    c = create_app(follower).test_client()
    assert c.get("/api/v1/ips/198.51.100.1", headers=BLOCK).status_code == 404


def test_new_tokens_are_readable_by_version_0_5(conn):
    auth.add_token(conn, "both", "all", "tester")
    auth.add_token(conn, "odd", "blocklist:read", "tester")
    scopes = dict(conn.execute("SELECT name, scope FROM api_tokens WHERE name IN ('both', 'odd')").fetchall())
    assert scopes == {"both": "all", "odd": "custom"}  # 0.5 knows 'all'; 'custom' it refuses


def test_the_permission_grid(monkeypatch, capsys):
    answers = iter(["4", "9", "", "1 3", "", "2", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    # all four, switch off whitelist write -> blocklist read+write, whitelist read
    assert admin.ask_permissions() == ("blocklist:read", "blocklist:write", "whitelist:read")
    assert "Type 1, 2, 3 or 4" in capsys.readouterr().out
    # switch off both reads -> the writes go too -> nothing left, then write ticks read again
    assert admin.ask_permissions(("blocklist:read", "blocklist:write", "whitelist:read")) == \
        ("blocklist:read", "blocklist:write")
    assert "Tick at least one box" in capsys.readouterr().out


def test_the_expiry_question(monkeypatch, capsys):
    answers = iter(["", "abc", "90", "", "7"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert admin.ask_expires_days() == 90  # no default: an empty answer is asked again
    assert capsys.readouterr().out.count("0 to 3650") == 2
    assert admin.ask_expires_days(keep=30) is None  # Enter keeps it
    assert admin.ask_expires_days(keep=30) == 7
