"""Furnace in a Docker container: settings from environment variables and a small process manager.

On a server, "furnace setup" asks questions and systemd runs everything. In a container
there is no systemd, so this module does both jobs:

    furnace-python -m furnace.container start       the container's main process
    furnace-python -m furnace.container check       only check the settings, change nothing
    furnace-python -m furnace.container replicate   send one snapshot to the other server

"start" checks the settings, prepares the secrets (leader) or takes them from the leader's
bundle (follower), writes the nginx and keepalived configuration, then starts the API,
nginx and keepalived. It restarts what stops, sends a snapshot to the other server every 20
seconds (leader only), makes the daily backup and runs the nightly cleanup (leader only).
On SIGTERM (docker stop) it stops keepalived first, so the other server takes the shared IP.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import string
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

LIB = Path("/usr/lib/furnace")
ETC = Path("/etc/furnace")
DATA = Path("/var/lib/furnace")
STATE = Path("/run/furnace-state")
PY = str(LIB / "bin" / "furnace-python")
NGINX_CONF = Path("/etc/nginx/conf.d/furnace.conf")
KEEPALIVED_CONF = Path("/etc/keepalived/keepalived.conf")
BUNDLE_NAME = "furnace-peer-bundle.tar.gz"
BUNDLE_FILES = ("tls/furnace.crt", "tls/furnace.key", "secrets/keepalived-password", "secrets/replica-secret")
USER = "furnace"
WEB_GROUP = "www-data"
REPLICATE_EVERY = 20  # seconds, as the timer on the servers
REPLICA_REMIND = 600  # seconds: while replication keeps failing, log a reminder this often
DAILY_JOBS = {"backup": (1, 30, False), "cleanup": (2, 30, True)}  # name: (hour, minute, leader only)


class SettingsError(Exception):
    """A setting is missing or wrong. The message says which and why."""


def log(message: str) -> None:
    print(f"furnace: {message}", flush=True)


# --------------------------------------------------------------------------- settings


@dataclass
class ContainerSettings:
    role: str
    this_ip: str
    peer_ip: str
    shared_ip: str
    shared_ip_prefix: int = 24
    interface: str = ""
    api_allow_from: str = "any"
    lists_allow_from: str = "any"
    vrrp_router_id: int = 51
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # shown at the start, for example "use the new names"

    @property
    def priority(self) -> int:
        return 150 if self.role == "leader" else 100


def _ipv4(value: str) -> bool:
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        return False
    return True


def _ip_list(name: str, value: str, problems: list[str]) -> str:
    value = value.strip() or "any"
    if value == "any":
        return value
    items = [item.strip() for item in value.split(",") if item.strip()]
    if not items:
        problems.append(f"FURNACE_{name} needs 'any' or at least one IP address.")
    for item in items:
        try:
            ipaddress.ip_network(item, strict=True)
        except ValueError:
            problems.append(f"FURNACE_{name} contains '{item}'. Use 'any' or IP addresses and ranges.")
    return ",".join(items)


def _number(name: str, value: str, default: int, low: int, high: int, problems: list[str]) -> int:
    if not value.strip():
        return default
    if not value.strip().isdigit() or not low <= int(value) <= high:
        problems.append(f"FURNACE_{name} must be a number from {low} to {high}, not '{value}'.")
        return default
    return int(value)


def addresses() -> dict[str, list[tuple[str, int]]]:
    """The IPv4 addresses of each network card: {card: [(ip, prefix), ...]} (from 'ip -o -4 addr')."""
    out = subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True, text=True, check=False).stdout
    cards: dict[str, list[tuple[str, int]]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[2] == "inet":
            ip, _, prefix = parts[3].partition("/")
            cards.setdefault(parts[1].split("@")[0], []).append((ip, int(prefix or 32)))
    return cards


def load_settings(env: dict[str, str] | None = None,
                  cards: dict[str, list[tuple[str, int]]] | None = None) -> ContainerSettings:
    """Read and check the FURNACE_ settings. Every problem is collected, then reported at once.

    The IPs: FURNACE_LEADER_IP and FURNACE_FOLLOWER_IP, the same on both hosts. The role is the one
    whose IP is on this host (FURNACE_ROLE is only needed when one host has both IPs). The names before
    0.10.0, FURNACE_THIS_IP and FURNACE_PEER_IP (with FURNACE_ROLE), still work.
    """
    env = dict(os.environ if env is None else env)
    get = lambda name: env.get(f"FURNACE_{name}", "").strip()  # noqa: E731
    problems: list[str] = []
    notes: list[str] = []
    cards = addresses() if cards is None else cards
    on_host = lambda ip: any(found == ip for card in cards.values() for found, _ in card)  # noqa: E731

    def address(name: str) -> str:
        value = get(name)
        if not value:
            problems.append(f"FURNACE_{name} is not set.")
        elif not _ipv4(value):
            problems.append(f"FURNACE_{name} '{value}' is not a valid IPv4 address.")
            return ""
        return value

    role = get("ROLE")
    if role and role not in ("leader", "follower"):
        problems.append(f"FURNACE_ROLE must be 'leader' or 'follower', not '{role}'.")
        role = ""
    old_names = bool(get("THIS_IP") or get("PEER_IP"))
    if old_names and (get("LEADER_IP") or get("FOLLOWER_IP")):
        problems.append("Use FURNACE_LEADER_IP and FURNACE_FOLLOWER_IP, or the old FURNACE_THIS_IP and "
                        "FURNACE_PEER_IP, not both.")
        this_ip = peer_ip = ""
        this_name = "FURNACE_THIS_IP"
    elif old_names:
        if not role and not get("ROLE"):
            problems.append("FURNACE_ROLE is not set. Use 'leader' on one host and 'follower' on the other.")
        this_ip, peer_ip = address("THIS_IP"), address("PEER_IP")
        this_name = "FURNACE_THIS_IP"
        if this_ip and this_ip == peer_ip:
            problems.append("FURNACE_PEER_IP must be the other host, not this host.")
        notes.append("FURNACE_THIS_IP and FURNACE_PEER_IP are the old names. Better: FURNACE_LEADER_IP and "
                     "FURNACE_FOLLOWER_IP, the same on both hosts (see .env.example).")
    else:
        leader_ip, follower_ip = address("LEADER_IP"), address("FOLLOWER_IP")
        if leader_ip and leader_ip == follower_ip:
            problems.append("FURNACE_LEADER_IP and FURNACE_FOLLOWER_IP must be the two different hosts.")
        mine = [r for r, ip in (("leader", leader_ip), ("follower", follower_ip)) if ip and on_host(ip)]
        if leader_ip and follower_ip and leader_ip != follower_ip and not role:
            if len(mine) == 1:
                role = mine[0]
            elif mine:
                problems.append("This host has both FURNACE_LEADER_IP and FURNACE_FOLLOWER_IP. "
                                "Add FURNACE_ROLE=leader or FURNACE_ROLE=follower.")
            else:
                problems.append(f"This host has neither {leader_ip} (FURNACE_LEADER_IP) nor {follower_ip} "
                                "(FURNACE_FOLLOWER_IP). Check the IPs with 'ip -br addr' on both hosts. "
                                "The container needs network_mode: host.")
        elif role and mine and role not in mine:
            problems.append(f"FURNACE_ROLE is {role}, but this host has the {mine[0]}'s IP "
                            f"({leader_ip if mine[0] == 'leader' else follower_ip}).")
        this_ip, peer_ip = (leader_ip, follower_ip) if role == "leader" else (follower_ip, leader_ip)
        if not role:
            this_ip = peer_ip = ""
        this_name = f"FURNACE_{role.upper()}_IP" if role else "FURNACE_LEADER_IP"
    shared_ip = address("SHARED_IP")
    if shared_ip and shared_ip in (this_ip, peer_ip, get("LEADER_IP"), get("FOLLOWER_IP")):
        problems.append("FURNACE_SHARED_IP must be a third address, not the IP of one of the hosts.")

    s = ContainerSettings(
        role=role, this_ip=this_ip, peer_ip=peer_ip, shared_ip=shared_ip, notes=notes,
        shared_ip_prefix=_number("SHARED_IP_PREFIX", get("SHARED_IP_PREFIX"), 24, 8, 32, problems),
        interface=get("INTERFACE"),
        api_allow_from=_ip_list("API_ALLOW_FROM", get("API_ALLOW_FROM"), problems),
        lists_allow_from=_ip_list("LISTS_ALLOW_FROM", get("LISTS_ALLOW_FROM"), problems),
        vrrp_router_id=_number("VRRP_ROUTER_ID", get("VRRP_ROUTER_ID"), 51, 1, 255, problems),
        problems=problems,
    )

    if s.interface:
        if re.fullmatch(r"[0-9.]+(/[0-9]+)?", s.interface) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", s.interface):
            problems.append(f"FURNACE_INTERFACE '{s.interface}' is not a network card name (see: ip -br addr).")
        elif s.interface not in cards:
            problems.append(f"The network card '{s.interface}' does not exist here. The container needs "
                            "network_mode: host, and the card name from 'ip -br addr' on the host.")
    elif _ipv4(s.this_ip):
        s.interface = next((card for card, found in cards.items() if any(ip == s.this_ip for ip, _ in found)), "")
    if _ipv4(s.this_ip) and s.interface in cards and not any(ip == s.this_ip for ip, _ in cards[s.interface]):
        problems.append(f"{this_name} {s.this_ip} is not set on the network card {s.interface}.")
    elif _ipv4(s.this_ip) and not s.interface:
        problems.append(f"{this_name} {s.this_ip} is not on any network card of this host. "
                        "The container needs network_mode: host.")
    if problems:
        raise SettingsError("\n".join(f"  - {p}" for p in problems))
    return s


# --------------------------------------------------------------------------- secrets


def _write_secret(path: Path, text: str, mode: int, group: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    os.chmod(tmp, mode)
    if group:
        shutil.chown(tmp, group=group)
    os.replace(tmp, path)


def new_keepalived_password() -> str:
    letters = string.ascii_letters + string.digits
    return "".join(secrets.choice(letters) for _ in range(8))  # keepalived uses only 8 characters


def cert_names(cert: Path) -> set[str]:
    out = subprocess.run(["openssl", "x509", "-in", str(cert), "-noout", "-ext", "subjectAltName"],
                         capture_output=True, text=True, check=False).stdout
    return set(re.findall(r"IP Address:([0-9.]+)", out))


def make_cert(s: ContainerSettings, etc: Path) -> None:
    (etc / "tls").mkdir(parents=True, exist_ok=True)
    os.chmod(etc / "tls", 0o700)
    key, crt = etc / "tls" / "furnace.key", etc / "tls" / "furnace.crt"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:3072", "-sha256", "-days", "3650", "-nodes",
         "-keyout", str(key), "-out", str(crt), "-subj", f"/CN=Furnace {s.shared_ip}/O=Furnace",
         "-addext", f"subjectAltName=IP:{s.shared_ip},IP:{s.this_ip},IP:{s.peer_ip},IP:127.0.0.1",
         "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
         "-addext", "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign",
         "-addext", "extendedKeyUsage=serverAuth"],
        check=True, capture_output=True)
    os.chmod(key, 0o600)
    os.chmod(crt, 0o644)


def have_secrets(etc: Path) -> bool:
    return all((etc / name).is_file() for name in BUNDLE_FILES)


def write_bundle(etc: Path) -> Path:
    bundle = etc / BUNDLE_NAME
    tmp = etc / f".{BUNDLE_NAME}.tmp"
    old = os.umask(0o077)
    try:
        with tarfile.open(tmp, "w:gz") as tar:
            for name in BUNDLE_FILES:
                tar.add(etc / name, arcname=name)
    finally:
        os.umask(old)
    os.replace(tmp, bundle)
    return bundle


def open_bundle(bundle: Path, etc: Path) -> None:
    """Copy the four secret files out of the leader's bundle. Nothing else in it is used."""
    try:
        with tarfile.open(bundle, "r:gz") as tar:
            members = {m.name: m for m in tar.getmembers() if m.isfile()}
            missing = [name for name in BUNDLE_FILES if name not in members]
            if missing:
                raise SettingsError(f"  - The bundle {bundle} has no {', '.join(missing)}. "
                                    "Copy the bundle from the leader again.")
            for name in BUNDLE_FILES:
                data = tar.extractfile(members[name]).read()  # type: ignore[union-attr]
                target = etc / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise SettingsError(f"  - The bundle {bundle} cannot be read ({exc}). Copy it from the leader again.") from None


def prepare_secrets(s: ContainerSettings, etc: Path = ETC, group: str | None = USER) -> list[str]:
    """Leader: make what is missing and write the bundle. Follower: take the secrets from the bundle."""
    notes: list[str] = []
    bundle = etc / BUNDLE_NAME
    if s.role == "leader":
        if not (etc / "tls" / "furnace.crt").is_file() or not (etc / "tls" / "furnace.key").is_file():
            make_cert(s, etc)
            notes.append(f"Self-signed certificate made (10 years) for {s.shared_ip}, {s.this_ip}, {s.peer_ip}.")
        if not (etc / "secrets" / "keepalived-password").is_file():
            _write_secret(etc / "secrets" / "keepalived-password", new_keepalived_password(), 0o600)
            notes.append("keepalived password made.")
        if not (etc / "secrets" / "replica-secret").is_file():
            _write_secret(etc / "secrets" / "replica-secret", secrets.token_urlsafe(32), 0o640, group)
            notes.append("Replication secret made.")
        write_bundle(etc)
        notes.append(f"Follower bundle: {bundle} (secret: copy it into the follower's /etc/furnace volume).")
    elif not have_secrets(etc):
        if not bundle.is_file():
            raise SettingsError(
                f"  - This follower has no secrets yet. Copy {BUNDLE_NAME} from the leader's /etc/furnace "
                f"volume into this host's /etc/furnace volume, then start the container again.")
        open_bundle(bundle, etc)
        bundle.unlink()
        notes.append("Certificate and secrets taken from the leader's bundle (bundle file deleted).")
    # The furnace user (replication) reads the certificate and the replication secret, never the key.
    os.chmod(etc / "tls", 0o750)
    os.chmod(etc / "tls" / "furnace.key", 0o600)
    os.chmod(etc / "tls" / "furnace.crt", 0o644)
    os.chmod(etc / "secrets", 0o750)
    os.chmod(etc / "secrets" / "keepalived-password", 0o600)
    os.chmod(etc / "secrets" / "replica-secret", 0o640)
    if group:
        for name in ("tls", "secrets", "secrets/replica-secret"):
            shutil.chown(etc / name, group=group)
    names = cert_names(etc / "tls" / "furnace.crt")
    missing = [ip for ip in (s.shared_ip, s.this_ip, s.peer_ip) if ip not in names]
    if missing:
        raise SettingsError(f"  - The certificate does not name {', '.join(missing)}. If the IPs changed: "
                            f"delete {etc}/tls and {etc}/secrets on the leader, start it, then copy the new "
                            "bundle to the follower (delete its tls and secrets folders first).")
    return notes


# --------------------------------------------------------------------------- configuration files


def allow_lines(value: str) -> str:
    if value == "any":
        return "        allow all;"
    return "\n".join(f"        allow {item};" for item in value.split(","))


def render(template: Path, values: dict[str, str]) -> str:
    text = template.read_text(encoding="utf-8")
    missing = sorted({name for name in re.findall(r"\{\{([A-Z0-9_]+)\}\}", text) if name not in values})
    if missing:
        raise SettingsError(f"  - Template {template} has no value for: {', '.join(missing)}")
    return re.sub(r"\{\{([A-Z0-9_]+)\}\}", lambda m: values[m.group(1)], text)


def template_values(s: ContainerSettings, etc: Path = ETC) -> dict[str, str]:
    password = (etc / "secrets" / "keepalived-password").read_text(encoding="utf-8").strip()
    return {
        "ROLE": s.role, "THIS_IP": s.this_ip, "PEER_IP": s.peer_ip, "SHARED_IP": s.shared_ip,
        "SHARED_IP_PREFIX": str(s.shared_ip_prefix), "INTERFACE": s.interface,
        "PRIORITY": str(s.priority), "VRRP_ROUTER_ID": str(s.vrrp_router_id), "KEEPALIVED_PASSWORD": password,
        "LISTS_ALLOW": allow_lines(s.lists_allow_from), "API_ALLOW": allow_lines(s.api_allow_from),
        "LISTEN": "    listen 443 ssl;",
        "TLS": (f"    ssl_certificate {etc}/tls/furnace.crt;\n"
                f"    ssl_certificate_key {etc}/tls/furnace.key;\n"
                "    ssl_protocols TLSv1.2 TLSv1.3;\n"
                "    ssl_session_cache shared:furnace_tls:5m;"),
        "REPLICA": ("    # Replication: only the other server may send a snapshot (with the replication secret).\n"
                    "    location = /replica/snapshot {\n"
                    f"        allow {s.peer_ip};\n"
                    "        deny all;\n"
                    "        client_max_body_size 2g;\n"
                    "        proxy_request_buffering off;\n"
                    "        proxy_pass http://127.0.0.1:8081/replica/snapshot;\n"
                    "        proxy_set_header X-Forwarded-For $remote_addr;\n"
                    "        proxy_read_timeout 120s;\n"
                    "    }\n"),
    }


def write_configs(s: ContainerSettings, etc: Path = ETC, lib: Path = LIB) -> None:
    values = template_values(s, etc)
    NGINX_CONF.write_text(render(lib / "templates" / "nginx-furnace.conf.tmpl", values), encoding="utf-8")
    old = os.umask(0o077)
    try:
        KEEPALIVED_CONF.write_text(render(lib / "templates" / "keepalived.conf.tmpl", values), encoding="utf-8")
    finally:
        os.umask(old)
    (etc / "install.env").write_text(
        "# Non-secret settings for the Furnace helper scripts. Written at every container start.\n"
        f"ROLE={s.role}\nTHIS_IP={s.this_ip}\nPEER_IP={s.peer_ip}\nSHARED_IP={s.shared_ip}\n"
        f"INTERFACE={s.interface}\nUSE_HTTPS=yes\n", encoding="utf-8")
    subprocess.run(["nginx", "-t", "-q"], check=True)


# --------------------------------------------------------------------------- replication (leader)


def replicate(peer_ip: str, etc: Path = ETC, data: Path = DATA, opener: Callable | None = None) -> str:
    """Send one snapshot to the other server. Returns a short result; raises on failure."""
    from .config import Settings
    from .replica import send_snapshot

    return send_snapshot(Settings.load(), peer_ip, etc / "tls" / "furnace.crt", etc / "secrets" / "replica-secret",
                         data / "replica" / "outgoing.db", opener=opener)


# --------------------------------------------------------------------------- process manager


@dataclass
class Process:
    name: str
    command: list[str]
    as_user: bool = False
    proc: subprocess.Popen | None = None
    started: float = 0.0
    restarts: int = 0


class Supervisor:
    def __init__(self, s: ContainerSettings):
        self.s = s
        self.stopping = False
        self.processes = [
            Process("api", [PY, "-m", "gunicorn", "--workers", "3", "--bind", "127.0.0.1:8081",
                            "--forwarded-allow-ips", "127.0.0.1",  # nginx logs the requests
                            "--timeout", "120", "--graceful-timeout", "5", "furnace.wsgi:app"], as_user=True),
            Process("nginx", ["nginx", "-g", "daemon off;"]),
            Process("keepalived", ["keepalived", "--dont-fork", "--log-console", "--log-detail",
                                   "--use-file", str(KEEPALIVED_CONF)]),
        ]
        self.job: tuple[str, subprocess.Popen] | None = None
        self.next_replica = time.monotonic() + 10
        self.replica_state = ""
        self.replica_failures = 0        # failed tries in a row
        self.replica_logged_ok = False   # the success was logged (once, until the next failure)
        self.replica_last_failure = ""   # the last failure text that was logged
        self.replica_last_log = 0.0      # when a failure was last logged (monotonic seconds)

    def spawn(self, p: Process) -> None:
        extra = {"user": USER, "group": USER, "umask": 0o027} if p.as_user else {}
        p.proc = subprocess.Popen(p.command, **extra)  # type: ignore[call-overload]
        p.started = time.monotonic()
        log(f"{p.name} started (pid {p.proc.pid}).")

    def run_job(self, name: str, command: list[str]) -> None:
        self.job = (name, subprocess.Popen(command, user=USER, group=USER, umask=0o027,
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True))

    def finish_job(self) -> None:
        if self.job is None or self.job[1].poll() is None:
            return
        name, proc = self.job
        self.job = None
        text = (proc.stdout.read() if proc.stdout else "").strip()
        if name == "replicate":
            line = self.note_replication(proc.returncode == 0, text.splitlines()[-1] if text else "")
            if line:
                log(line)
        else:
            for line in text.splitlines():
                log(f"{name}: {line}")
            log(f"{name} {'done' if proc.returncode == 0 else f'FAILED (exit {proc.returncode})'}.")

    def note_replication(self, ok: bool, state: str, now: float | None = None) -> str:
        """Remember the result of one replication. Returns the line to log, or "" for nothing.

        It runs every 20 seconds, so only changes are logged: the first success, the first failure,
        a different failure, and back to normal. While the other host stays down, one reminder
        every REPLICA_REMIND seconds instead of a line every 20 seconds."""
        now = time.monotonic() if now is None else now
        self.replica_state = ("ok " if ok else "failed ") + state
        if ok:
            failures, self.replica_failures = self.replica_failures, 0
            if failures:
                self.replica_logged_ok = True
                return f"replication works again after {failures} failed tries: {state or 'no output'}"
            if not self.replica_logged_ok:
                self.replica_logged_ok = True
                return f"replication: {state or 'no output'}"
            return ""
        self.replica_failures += 1
        self.replica_logged_ok = False
        if (self.replica_failures == 1 or state != self.replica_last_failure
                or now - self.replica_last_log >= REPLICA_REMIND):
            self.replica_last_failure, self.replica_last_log = state, now
            if self.replica_failures == 1:
                return f"replication FAILED: {state}"
            return (f"replication still FAILED ({self.replica_failures} tries in a row): {state}. "
                    f"Next reminder in {REPLICA_REMIND // 60} minutes unless it changes.")
        return ""

    def role(self) -> str:
        try:
            return (STATE / "role").read_text(encoding="utf-8").strip()
        except OSError:
            return "follower"

    def due_daily(self, name: str, hour: int, minute: int) -> bool:
        """Like a systemd timer with Persistent=true: a missed run happens after a restart."""
        now = datetime.now()
        stamp = DATA / f".last-{name}"
        last = stamp.read_text(encoding="utf-8").strip() if stamp.is_file() else ""
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now < due or last == now.strftime("%Y-%m-%d"):
            return False
        stamp.write_text(now.strftime("%Y-%m-%d") + "\n", encoding="utf-8")
        return True

    def schedule(self) -> None:
        self.finish_job()
        if self.job is not None:
            return
        leader = self.role() == "leader"
        for name, (hour, minute, leader_only) in DAILY_JOBS.items():
            if (leader or not leader_only) and self.due_daily(name, hour, minute):
                self.run_job(name, [PY, "-m", "furnace", name])
                return
        if time.monotonic() >= self.next_replica:
            self.next_replica = time.monotonic() + REPLICATE_EVERY
            if leader:
                self.run_job("replicate", [PY, "-m", "furnace.container", "replicate"])

    def write_status(self) -> None:
        rows = {p.name: ("running" if p.proc and p.proc.poll() is None else "stopped", p.restarts)
                for p in self.processes}
        status = {"processes": rows, "replication": self.replica_state, "role": self.role(),
                  "updated": datetime.now().isoformat(timespec="seconds")}
        tmp = STATE / ".status.tmp"
        tmp.write_text(json.dumps(status), encoding="utf-8")
        os.replace(tmp, STATE / "status.json")

    def stop(self, *_: object) -> None:
        self.stopping = True

    def shutdown(self) -> None:
        log("Stopping (keepalived first, so the other server takes the shared IP).")
        order = {"keepalived": signal.SIGTERM, "nginx": signal.SIGQUIT, "api": signal.SIGTERM}
        for name in ("keepalived", "nginx", "api"):
            p = next(x for x in self.processes if x.name == name)
            if p.proc and p.proc.poll() is None:
                p.proc.send_signal(order[name])
                try:
                    p.proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    p.proc.kill()
        if self.job is not None:
            self.job[1].kill()
        log("Stopped.")

    def wait_api(self, seconds: int = 60) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen("http://127.0.0.1:8081/health", timeout=3):
                    return
            except (urllib.error.URLError, OSError):
                time.sleep(0.5)
        raise SettingsError("  - The API did not start within a minute. See the messages above.")

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        api, nginx, keepalived = self.processes
        self.spawn(api)
        self.wait_api()
        self.spawn(nginx)
        self.spawn(keepalived)
        log(f"Ready. This host is the {self.s.role} by setting; keepalived decides who owns {self.s.shared_ip}.")
        while not self.stopping:
            for p in self.processes:
                if p.proc is not None and p.proc.poll() is not None and not self.stopping:
                    log(f"{p.name} stopped (exit {p.proc.returncode}). Starting it again in 2 seconds.")
                    time.sleep(2)
                    p.restarts += 1
                    self.spawn(p)
            self.schedule()
            self.write_status()
            time.sleep(1)
        self.shutdown()
        return 0


# --------------------------------------------------------------------------- start


def prepare_folders() -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE, 0o755)
    role = STATE / "role"
    role.write_text("follower\n", encoding="utf-8")  # keepalived writes leader when it takes the shared IP
    os.chmod(role, 0o644)
    ETC.mkdir(parents=True, exist_ok=True)
    os.chmod(ETC, 0o750)
    shutil.chown(ETC, group=USER)
    for path in (DATA, Path(os.environ.get("FURNACE_EDL_DIR", "/var/lib/furnace-edl"))):
        path.mkdir(parents=True, exist_ok=True)
    auth = Path(os.environ.get("FURNACE_AUTH_DIR", "/var/lib/furnace-auth"))
    auth.mkdir(parents=True, exist_ok=True)
    # The data volume may come from the host with other owners: give it to the furnace user.
    subprocess.run(["chown", "-R", f"{USER}:{USER}", str(DATA)], check=True)
    os.chmod(DATA, 0o750)
    shutil.chown(Path(os.environ.get("FURNACE_EDL_DIR", "/var/lib/furnace-edl")), USER, USER)
    shutil.chown(auth, USER, WEB_GROUP)
    os.chmod(auth, 0o2750)  # nginx reads the list users file


REFUSE_TEXT = """\
# Names that may never go on the domain whitelist, one per line. Furnace also refuses every shared platform
# and public name of the Public Suffix List (github.io, blogspot.com, co.uk, ...), which comes with Furnace.
# A name here also refuses every name above it: drive.google.com here refuses google.com too, because a
# whitelisted domain protects all its subdomains. Keep this file the same on both servers.
# Examples (remove the # to use them):
# dropbox.com
# drive.google.com
"""


def write_refuse_file(etc: Path = ETC) -> str:
    """The admin's own names that may never be whitelisted. Made once, empty; never changed afterwards."""
    path = etc / "whitelist-refuse.txt"
    if path.exists():
        return ""
    path.write_text(REFUSE_TEXT, encoding="utf-8")
    os.chmod(path, 0o644)
    return f"Wrote {path} (your own names that may never be whitelisted; empty)."


def start() -> int:
    log("Starting Furnace " + _version() + " (Docker).")
    try:
        s = load_settings()
        log(f"This host is the {s.role} ({s.this_ip}); the other host is {s.peer_ip}.")
        for note in s.notes:
            log(note)
        prepare_folders()
        for note in prepare_secrets(s):
            log(note)
        note = write_refuse_file()
        if note:
            log(note)
        write_configs(s)
    except SettingsError as exc:
        log(f"Cannot start. Fix these settings, then start the container again:\n{exc}")
        return 2
    except subprocess.CalledProcessError as exc:
        log(f"Cannot start: {' '.join(map(str, exc.cmd))} failed (exit {exc.returncode}).")
        return 2
    rebuild = subprocess.run([PY, "-m", "furnace", "rebuild"], user=USER, group=USER, umask=0o027,
                             capture_output=True, text=True)
    if rebuild.returncode != 0:
        log(f"Cannot start: the database check failed.\n{rebuild.stdout}{rebuild.stderr}")
        return 2
    log("Database ready and lists written (existing data kept).")
    try:
        return Supervisor(s).run()
    except SettingsError as exc:
        log(f"Cannot start:\n{exc}")
        return 2


def _version() -> str:
    from . import __version__
    return __version__


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "start"
    if command == "start":
        return start()
    if command == "check":
        try:
            s = load_settings()
        except SettingsError as exc:
            print(f"Settings are not right:\n{exc}")
            return 2
        print(f"Settings are fine: this host is the {s.role} ({s.this_ip} on {s.interface}), "
              f"the other host is {s.peer_ip}, shared IP {s.shared_ip}/{s.shared_ip_prefix}.")
        for note in s.notes:
            print(f"Note: {note}")
        return 0
    if command == "replicate":
        try:
            print(replicate(load_settings().peer_ip))
        except Exception as exc:  # one line for the log; the next try is in 20 seconds
            print(str(exc) or exc.__class__.__name__)
            return 1
        return 0
    print("usage: python -m furnace.container [start|check|replicate]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
