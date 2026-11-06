"""The join code: the follower fetches the leader's secrets over HTTPS, instead of a copied bundle file.

On the leader (root):  python -m furnace.join make --bundle FILE --cert FILE --dir DIR [--owner furnace]
    prints a one-time code, for example  FJ1-ABCD-EFGH-IJKL-MNOP-QRST-UVWX-YZ23-4567
On the follower:       python -m furnace.join fetch --code CODE --out FILE [--leader IP]
    writes the leader's bundle to FILE (the same file the bundle copy gives).

The code holds three things (20 bytes, base32):
  - a random secret (8 bytes). The bundle waits on the leader ENCRYPTED with a key made from it, so the API
    (which runs as the furnace user and may not read the TLS key) never sees the secrets in clear text.
    The follower sends only a proof made from the secret, never the secret itself.
  - the first 8 bytes of the SHA-256 of the leader's certificate. The follower checks it before it sends
    anything, so a fake server cannot answer in the leader's place.
  - the leader's IP (4 bytes), so the follower knows where to ask.
The code works once, for one hour, and only from the follower's IP (nginx). After 5 wrong proofs it is gone.

Only the standard library: the encryption is HMAC-SHA256 in counter mode, then HMAC-SHA256 over the result
(encrypt-then-MAC), each with its own key made from the secret.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import fcntl
import hashlib
import hmac
import http.client
import io
import ipaddress
import json
import os
import secrets
import shutil
import socket
import ssl
import sys
import tarfile
import time
from pathlib import Path

PREFIX = "FJ1"
LIFETIME = 3600       # seconds
MAX_TRIES = 5
SECRET_BYTES = 8
FINGERPRINT_BYTES = 8
CODE_FILE = "code.json"
BLOB_FILE = "bundle.enc"
LOCK_FILE = "lock"


class JoinError(Exception):
    """Something about the join code went wrong. The message says what to do. http is the API status."""

    def __init__(self, message: str, http: int = 400):
        super().__init__(message)
        self.http = http


# --------------------------------------------------------------------------- the code


def fingerprint(cert_der: bytes) -> bytes:
    return hashlib.sha256(cert_der).digest()[:FINGERPRINT_BYTES]


def cert_der(path: Path) -> bytes:
    return ssl.PEM_cert_to_DER_cert(Path(path).read_text(encoding="ascii"))


def format_code(secret: bytes, fp: bytes, leader_ip: str) -> str:
    raw = secret + fp + ipaddress.IPv4Address(leader_ip).packed
    text = base64.b32encode(raw).decode("ascii")  # 20 bytes: 32 letters, no padding
    return "-".join([PREFIX] + [text[i:i + 4] for i in range(0, len(text), 4)])


def parse_code(code: str) -> tuple[bytes, bytes, str]:
    """(secret, fingerprint, leader IP) from a code. Spaces, dashes and small letters are fine."""
    text = "".join(ch for ch in code.upper() if ch.isalnum())
    if not text.startswith(PREFIX):
        raise JoinError("This is not a Furnace join code (it starts with FJ1-). Copy it again from the leader.")
    text = text[len(PREFIX):].replace("0", "O").replace("1", "I").replace("8", "B")
    try:
        raw = base64.b32decode(text)
    except (ValueError, binascii.Error):
        raw = b""
    if len(raw) != SECRET_BYTES + FINGERPRINT_BYTES + 4:
        raise JoinError("The join code is not complete or has a typo. Copy it again from the leader.")
    secret, fp = raw[:SECRET_BYTES], raw[SECRET_BYTES:SECRET_BYTES + FINGERPRINT_BYTES]
    return secret, fp, str(ipaddress.IPv4Address(raw[-4:]))


def _key(secret: bytes, purpose: str) -> bytes:
    return hmac.new(secret, f"furnace-join-{purpose}".encode(), hashlib.sha256).digest()


def proof_of(secret: bytes) -> str:
    """What the follower sends. The leader keeps only this, never the secret."""
    return _key(secret, "proof").hex()


def _keystream(key: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def encrypt(data: bytes, secret: bytes) -> bytes:
    nonce = secrets.token_bytes(16)
    stream = _keystream(_key(secret, "enc") + nonce, len(data))
    body = nonce + bytes(a ^ b for a, b in zip(data, stream))
    return body + hmac.new(_key(secret, "mac"), body, hashlib.sha256).digest()


def decrypt(blob: bytes, secret: bytes) -> bytes:
    body, tag = blob[:-32], blob[-32:]
    if len(blob) < 48 or not hmac.compare_digest(tag, hmac.new(_key(secret, "mac"), body, hashlib.sha256).digest()):
        raise JoinError("The answer from the leader is damaged or was not made for this code. Make a new code.")
    nonce, data = body[:16], body[16:]
    stream = _keystream(_key(secret, "enc") + nonce, len(data))
    return bytes(a ^ b for a, b in zip(data, stream))


# --------------------------------------------------------------------------- leader: make


def make(bundle: bytes, cert: bytes, leader_ip: str, join_dir: Path, owner: str | None = None,
         now: float | None = None) -> str:
    """Put the encrypted bundle in join_dir and return the code. A new code replaces the old one."""
    now = time.time() if now is None else now
    secret = secrets.token_bytes(SECRET_BYTES)
    join_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(join_dir, 0o700)
    if owner:
        shutil.chown(join_dir, user=owner, group=owner)
    for name, data in ((BLOB_FILE, encrypt(bundle, secret)),
                       (CODE_FILE, json.dumps({"expires": int(now) + LIFETIME, "proof": proof_of(secret),
                                               "tries": 0}).encode())):
        tmp = join_dir / f".{name}.tmp"
        tmp.write_bytes(data)
        os.chmod(tmp, 0o600)
        if owner:
            shutil.chown(tmp, user=owner, group=owner)
        os.replace(tmp, join_dir / name)
    return format_code(secret, fingerprint(cert), leader_ip)


def remove(join_dir: Path) -> None:
    for name in (CODE_FILE, BLOB_FILE):
        try:
            (join_dir / name).unlink()
        except FileNotFoundError:
            pass


# --------------------------------------------------------------------------- leader: the API hands it out


def claim(join_dir: Path, proof: str, now: float | None = None) -> bytes:
    """The encrypted bundle for a right proof, once. Raises JoinError with the HTTP status otherwise."""
    now = time.time() if now is None else now
    if not (join_dir / CODE_FILE).is_file():
        raise JoinError("No join code is waiting on the leader. Make one there: furnace join-code", 404)
    fd = os.open(join_dir / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            state = json.loads((join_dir / CODE_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise JoinError("No join code is waiting on the leader. Make one there: furnace join-code", 404) from None
        if now > state["expires"]:
            remove(join_dir)
            raise JoinError("The join code is older than 1 hour. Make a new one on the leader: furnace join-code", 410)
        if not isinstance(proof, str) or not hmac.compare_digest(proof.encode(), state["proof"].encode()):
            state["tries"] += 1
            if state["tries"] >= MAX_TRIES:
                remove(join_dir)
                raise JoinError(f"{MAX_TRIES} wrong join codes: the code is gone. Make a new one on the leader: "
                                "furnace join-code", 403)
            (join_dir / CODE_FILE).write_text(json.dumps(state), encoding="utf-8")
            raise JoinError("The join code is wrong. Copy it again from the leader.", 403)
        blob = (join_dir / BLOB_FILE).read_bytes()
        remove(join_dir)
        return blob
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# --------------------------------------------------------------------------- follower: fetch


def bundle_cert(bundle: bytes) -> bytes:
    """The DER certificate inside a bundle (tls/furnace.crt)."""
    try:
        with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as tar:
            member = tar.extractfile("tls/furnace.crt")
            if member is None:
                raise KeyError
            return ssl.PEM_cert_to_DER_cert(member.read().decode("ascii"))
    except (KeyError, tarfile.TarError, ValueError, OSError, EOFError):
        raise JoinError("The leader sent no usable bundle. Make a new code on the leader.") from None


def fetch(code: str, leader_ip: str | None = None, port: int = 443, timeout: float = 15.0) -> bytes:
    """Ask the leader for the bundle. Checks the leader's certificate against the code first."""
    secret, fp, code_ip = parse_code(code)
    if leader_ip and leader_ip != code_ip:
        raise JoinError(f"The join code was made by {code_ip}, but the leader here is {leader_ip}. "
                        "Make the code on the leader, or check the leader's IP.")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # the code's fingerprint is the check: a self-signed certificate
    conn = http.client.HTTPSConnection(code_ip, port, context=ctx, timeout=timeout)
    try:
        try:
            conn.connect()
        except (OSError, socket.timeout) as exc:
            raise JoinError(f"Cannot reach the leader at https://{code_ip}:{port} ({exc}). Is it running, and "
                            "does its firewall let this server in?", 503) from None
        der = conn.sock.getpeercert(binary_form=True)  # type: ignore[union-attr]
        if not der or not hmac.compare_digest(fingerprint(der), fp):
            raise JoinError(f"The server at {code_ip} is not the leader that made this code (its certificate is "
                            "different). Nothing was sent.", 495)
        conn.request("POST", "/join", body=json.dumps({"proof": proof_of(secret)}),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read()
    except (OSError, http.client.HTTPException) as exc:
        raise JoinError(f"The leader at {code_ip} did not answer ({exc}).", 503) from None
    finally:
        conn.close()
    if resp.status != 200:
        try:
            message = json.loads(data)["message"]
        except (ValueError, KeyError, TypeError):
            message = f"HTTP {resp.status}" + (" (only the follower's IP may ask)" if resp.status == 403 else "")
        raise JoinError(f"The leader said: {message}", resp.status)
    bundle = decrypt(data, secret)
    if not hmac.compare_digest(fingerprint(bundle_cert(bundle)), fp):
        raise JoinError("The bundle from the leader has another certificate than the code. Make a new code.")
    return bundle


# --------------------------------------------------------------------------- command line


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m furnace.join")
    sub = parser.add_subparsers(dest="command", required=True)
    m = sub.add_parser("make")
    m.add_argument("--bundle", required=True, help="the bundle file, or - for standard input")
    m.add_argument("--cert", required=True)
    m.add_argument("--leader-ip", required=True)
    m.add_argument("--dir", required=True)
    m.add_argument("--owner")
    f = sub.add_parser("fetch")
    f.add_argument("--code", required=True)
    f.add_argument("--out", required=True)
    f.add_argument("--leader")
    f.add_argument("--port", type=int, default=443)
    args = parser.parse_args(argv)
    try:
        if args.command == "make":
            bundle = sys.stdin.buffer.read() if args.bundle == "-" else Path(args.bundle).read_bytes()
            print(make(bundle, cert_der(Path(args.cert)), args.leader_ip, Path(args.dir), args.owner))
        else:
            data = fetch(args.code, args.leader, args.port)
            old = os.umask(0o077)
            try:
                Path(args.out).write_bytes(data)
            finally:
                os.umask(old)
    except JoinError as exc:
        print(exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
