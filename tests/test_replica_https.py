"""Replication over HTTPS (Docker): the leader POSTs a gzip snapshot to /replica/snapshot."""

import gzip
import io
import os
from pathlib import Path

import pytest

from furnace import backup, replica
from furnace.api import create_app
from furnace.config import Settings
from furnace.service import Furnace

SECRET = "replica-secret-0123456789abcdef0123456789"


def follower_settings(tmp_path: Path, secret_file: Path | None) -> Settings:
    f = tmp_path / "follower"
    (tmp_path / "role").write_text("follower\n")
    s = Settings(data_dir=f, edl_dir=f / "edl", archive_dir=f / "archive", backup_dir=f / "backups",
                 auth_dir=f / "auth", role_file=tmp_path / "role", allow_test_ranges=True,
                 replica_secret_file=secret_file)
    s.ensure_dirs()
    Furnace(s).startup()
    return s


@pytest.fixture
def secret_file(tmp_path: Path) -> Path:
    path = tmp_path / "replica-secret"
    path.write_text(SECRET + "\n")
    return path


@pytest.fixture
def snapshot_gz(settings, tmp_path) -> bytes:
    leader = Furnace(settings)
    leader.add_ip("198.51.100.1", permanent=True)
    leader.add_domain("bad.example.com")
    snap = backup.snapshot(settings, tmp_path / "snap.db")
    return gzip.compress(Path(snap).read_bytes())


def post(client, data: bytes, secret: str = SECRET):
    headers = {"Authorization": f"Bearer {secret}"} if secret else {}
    return client.post("/replica/snapshot", data=data, headers=headers,
                       content_type="application/octet-stream")


def test_the_follower_applies_a_snapshot(tmp_path, secret_file, snapshot_gz):
    s = follower_settings(tmp_path, secret_file)
    r = post(create_app(s).test_client(), snapshot_gz)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["applied"]["ips"] == 1
    assert s.blocklist_path.read_text() == "198.51.100.1\n"
    assert s.domains_path.read_text() == "bad.example.com\n"
    assert list(s.incoming_dir.iterdir()) == []  # nothing left behind


def test_larger_than_the_normal_request_limit(tmp_path, secret_file, settings):
    leader = Furnace(settings)
    leader.add_ip("198.51.100.1")
    snap = Path(backup.snapshot(settings, tmp_path / "snap.db")).read_bytes()
    # Uncompressible padding after the database pages is ignored by SQLite but makes the body big.
    body = gzip.compress(snap + os.urandom(21 * 1024 * 1024), compresslevel=1)
    assert len(body) > 20 * 1024 * 1024
    s = follower_settings(tmp_path, secret_file)
    r = post(create_app(s).test_client(), body)
    assert r.status_code == 200, r.get_json()


@pytest.mark.parametrize("secret", ["", "wrong-secret-0123456789abcdef0123456789", SECRET[:-1]])
def test_a_wrong_secret_is_refused(tmp_path, secret_file, snapshot_gz, secret):
    s = follower_settings(tmp_path, secret_file)
    r = post(create_app(s).test_client(), snapshot_gz, secret=secret)
    assert r.status_code == 401
    assert s.blocklist_path.read_text() == ""


def test_a_short_secret_file_refuses_everything(tmp_path, snapshot_gz):
    short = tmp_path / "short"
    short.write_text("short\n")
    s = follower_settings(tmp_path, short)
    assert post(create_app(s).test_client(), snapshot_gz, secret="short").status_code == 401


@pytest.mark.parametrize("body", [b"not gzip at all", gzip.compress(b"gzip, but not a database")])
def test_bad_data_is_dropped(tmp_path, secret_file, body):
    s = follower_settings(tmp_path, secret_file)
    r = post(create_app(s).test_client(), body)
    assert r.status_code == 422
    assert list(s.incoming_dir.iterdir()) == []


def test_the_leader_refuses_a_snapshot(tmp_path, secret_file, snapshot_gz):
    s = follower_settings(tmp_path, secret_file)
    (tmp_path / "role").write_text("leader\n")
    r = post(create_app(s).test_client(), snapshot_gz)
    assert r.status_code == 409
    assert s.blocklist_path.read_text() == ""


def test_too_large_after_unpacking(tmp_path, secret_file):
    s = follower_settings(tmp_path, secret_file)
    body = gzip.compress(b"SQLite format 3\x00" + bytes(2048))
    with pytest.raises(replica.ReplicaError, match="too large"):
        replica.receive_snapshot(s, io.BytesIO(body), limit=1024)
    assert list(s.incoming_dir.iterdir()) == []


def test_off_without_a_secret_file(tmp_path, snapshot_gz):
    s = follower_settings(tmp_path, None)
    assert post(create_app(s).test_client(), snapshot_gz).status_code == 404


def test_secret_file_from_the_environment(tmp_path):
    s = Settings.from_env({"FURNACE_DATA_DIR": str(tmp_path), "FURNACE_REPLICA_SECRET_FILE": "/etc/furnace/replica-secret"})
    assert s.replica_secret_file == Path("/etc/furnace/replica-secret")
    assert s.incoming_dir == tmp_path / "incoming"
    assert Settings.from_env({}).replica_secret_file is None
