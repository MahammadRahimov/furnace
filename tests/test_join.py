"""The join code: the follower fetches the leader's bundle over HTTPS (furnace.join and POST /join)."""

from __future__ import annotations

import io
import json
import shutil
import ssl
import subprocess
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from furnace import join
from furnace.api import create_app

needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")
LEADER_IP = "127.0.0.1"


def make_cert(folder: Path, name: str = "furnace") -> tuple[Path, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    key, crt = folder / f"{name}.key", folder / f"{name}.crt"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-keyout", str(key),
                    "-out", str(crt), "-subj", "/CN=Furnace test", "-addext", "subjectAltName=IP:127.0.0.1"],
                   check=True, capture_output=True)
    return key, crt


def bundle_with(crt: Path) -> bytes:
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as tar:
        tar.add(crt, arcname="tls/furnace.crt")
        info = tarfile.TarInfo("secrets/keepalived-password")
        info.size = 9
        tar.addfile(info, io.BytesIO(b"abcd1234\n"))
    return data.getvalue()


FAKE_CERT = b"not really a certificate, only bytes for the fingerprint"


def test_the_code_round_trip_and_typos():
    code = join.format_code(b"\x01" * 8, b"\x02" * 8, "192.0.2.11")
    assert code.startswith("FJ1-") and len(code.split("-")) == 9
    assert join.parse_code(code) == (b"\x01" * 8, b"\x02" * 8, "192.0.2.11")
    # Small letters, spaces, no dashes, and 0/1/8 typed for O/I/B still work.
    messy = " " + code.lower().replace("-", " ") + " "
    assert join.parse_code(messy) == join.parse_code(code)
    with pytest.raises(join.JoinError, match="not a Furnace join code"):
        join.parse_code("ABCD-EFGH")
    with pytest.raises(join.JoinError, match="not complete or has a typo"):
        join.parse_code(code[:-5])


def test_encrypt_and_decrypt():
    secret = b"s" * 8
    blob = join.encrypt(b"the bundle" * 100, secret)
    assert b"the bundle" not in blob
    assert join.decrypt(blob, secret) == b"the bundle" * 100
    with pytest.raises(join.JoinError, match="damaged"):
        join.decrypt(blob, b"x" * 8)
    broken = bytearray(blob)
    broken[20] ^= 1
    with pytest.raises(join.JoinError, match="damaged"):
        join.decrypt(bytes(broken), secret)


def test_make_and_claim_once(tmp_path):
    code = join.make(b"BUNDLE", FAKE_CERT, "192.0.2.11", tmp_path / "join", now=1000)
    secret, fp, ip = join.parse_code(code)
    assert (fp, ip) == (join.fingerprint(FAKE_CERT), "192.0.2.11")
    for name in ("code.json", "bundle.enc"):
        assert (tmp_path / "join" / name).stat().st_mode & 0o777 == 0o600
    assert b"BUNDLE" not in (tmp_path / "join" / "bundle.enc").read_bytes()
    assert secret.hex() not in (tmp_path / "join" / "code.json").read_text()
    blob = join.claim(tmp_path / "join", join.proof_of(secret), now=1000 + 3599)
    assert join.decrypt(blob, secret) == b"BUNDLE"
    with pytest.raises(join.JoinError, match="No join code is waiting") as info:
        join.claim(tmp_path / "join", join.proof_of(secret), now=1001)
    assert info.value.http == 404


def test_a_code_expires_after_one_hour(tmp_path):
    code = join.make(b"BUNDLE", FAKE_CERT, "192.0.2.11", tmp_path, now=1000)
    with pytest.raises(join.JoinError, match="older than 1 hour") as info:
        join.claim(tmp_path, join.proof_of(join.parse_code(code)[0]), now=1000 + 3601)
    assert info.value.http == 410
    assert not (tmp_path / "bundle.enc").exists()


def test_five_wrong_codes_and_it_is_gone(tmp_path):
    code = join.make(b"BUNDLE", FAKE_CERT, "192.0.2.11", tmp_path, now=1000)
    for _ in range(4):
        with pytest.raises(join.JoinError, match="The join code is wrong"):
            join.claim(tmp_path, "00" * 32, now=1001)
    # Four wrong tries: the right one still works ...
    assert join.claim(tmp_path, join.proof_of(join.parse_code(code)[0]), now=1001)
    code = join.make(b"BUNDLE", FAKE_CERT, "192.0.2.11", tmp_path, now=1000)
    for _ in range(4):
        with pytest.raises(join.JoinError):
            join.claim(tmp_path, "00" * 32, now=1001)
    # ... the fifth wrong one removes the code.
    with pytest.raises(join.JoinError, match="5 wrong join codes") as info:
        join.claim(tmp_path, "00" * 32, now=1001)
    assert info.value.http == 403
    with pytest.raises(join.JoinError, match="No join code"):
        join.claim(tmp_path, join.proof_of(join.parse_code(code)[0]), now=1001)


def test_a_new_code_replaces_the_old_one(tmp_path):
    old = join.make(b"OLD", FAKE_CERT, "192.0.2.11", tmp_path, now=1000)
    new = join.make(b"NEW", FAKE_CERT, "192.0.2.11", tmp_path, now=1000)
    with pytest.raises(join.JoinError, match="wrong"):
        join.claim(tmp_path, join.proof_of(join.parse_code(old)[0]), now=1001)
    secret = join.parse_code(new)[0]
    assert join.decrypt(join.claim(tmp_path, join.proof_of(secret), now=1001), secret) == b"NEW"


def test_the_api_route(settings):
    code = join.make(b"BUNDLE", FAKE_CERT, "192.0.2.11", settings.join_dir)
    secret = join.parse_code(code)[0]
    client = create_app(settings).test_client()
    r = client.post("/join", json={"proof": "00" * 32})
    assert r.status_code == 403 and "wrong" in r.get_json()["message"]
    r = client.post("/join", data="not json", content_type="text/plain")
    assert r.status_code == 403
    r = client.post("/join", json={"proof": join.proof_of(secret)})
    assert r.status_code == 200 and join.decrypt(r.data, secret) == b"BUNDLE"
    r = client.post("/join", json={"proof": join.proof_of(secret)})
    assert r.status_code == 404 and "furnace join-code" in r.get_json()["message"]


# --------------------------------------------------------------------------- fetch over real TLS


class Leader:
    """A tiny HTTPS server that answers POST /join like nginx + the API on the leader."""

    def __init__(self, folder: Path, key: Path, crt: Path):
        self.folder = folder
        self.requests = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                outer.requests += 1
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                try:
                    data, status = join.claim(outer.folder, body["proof"]), 200
                except join.JoinError as exc:
                    data, status = json.dumps({"message": str(exc)}).encode(), exc.http
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(crt, key)
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()


@needs_openssl
def test_fetch_from_the_leader(tmp_path):
    key, crt = make_cert(tmp_path / "tls")
    bundle = bundle_with(crt)
    code = join.make(bundle, join.cert_der(crt), LEADER_IP, tmp_path / "join")
    leader = Leader(tmp_path / "join", key, crt)
    try:
        assert join.fetch(code, LEADER_IP, port=leader.port) == bundle
        with pytest.raises(join.JoinError, match="The leader said: No join code is waiting"):
            join.fetch(code, port=leader.port)
    finally:
        leader.stop()


@needs_openssl
def test_a_fake_leader_gets_nothing(tmp_path):
    _, real_crt = make_cert(tmp_path / "real")
    fake_key, fake_crt = make_cert(tmp_path / "fake")
    code = join.make(bundle_with(real_crt), join.cert_der(real_crt), LEADER_IP, tmp_path / "join")
    fake = Leader(tmp_path / "join", fake_key, fake_crt)
    try:
        with pytest.raises(join.JoinError, match="not the leader that made this code") as info:
            join.fetch(code, port=fake.port)
        assert info.value.http == 495
        assert fake.requests == 0  # nothing was sent to it, not even the proof
    finally:
        fake.stop()


def test_fetch_checks_the_leader_ip_and_says_when_it_is_not_reachable():
    code = join.format_code(b"\x01" * 8, b"\x02" * 8, "127.0.0.1")
    with pytest.raises(join.JoinError, match="made by 127.0.0.1, but the leader here is 192.0.2.11"):
        join.fetch(code, "192.0.2.11")
    with pytest.raises(join.JoinError, match="Cannot reach the leader"):
        join.fetch(code, port=1, timeout=2)


@needs_openssl
def test_the_command_line(tmp_path, capsys):
    key, crt = make_cert(tmp_path / "tls")
    bundle_file = tmp_path / "bundle.tar.gz"
    bundle_file.write_bytes(bundle_with(crt))
    assert join.main(["make", "--bundle", str(bundle_file), "--cert", str(crt), "--leader-ip", LEADER_IP,
                      "--dir", str(tmp_path / "join")]) == 0
    code = capsys.readouterr().out.strip()
    leader = Leader(tmp_path / "join", key, crt)
    try:
        out = tmp_path / "got.tar.gz"
        assert join.main(["fetch", "--code", code, "--out", str(out), "--port", str(leader.port)]) == 0
        assert out.read_bytes() == bundle_file.read_bytes()
        assert out.stat().st_mode & 0o777 == 0o600
        assert join.main(["fetch", "--code", code, "--out", str(out), "--port", str(leader.port)]) == 2
        assert "No join code is waiting" in capsys.readouterr().err
    finally:
        leader.stop()
