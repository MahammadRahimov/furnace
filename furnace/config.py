"""Settings for Furnace.

All settings come from environment variables that start with FURNACE_.
On a server they live in /etc/furnace/furnace.env, which systemd loads
for the services and which the furnace command reads on its own.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_FILE = "/etc/furnace/furnace.env"
MIN_SELFTEST_TOKEN_LENGTH = 20
SELFTEST_FLAG_MAX_AGE = 600  # seconds


class ConfigError(Exception):
    """A setting is missing or wrong."""


def read_env_file(path: str | os.PathLike) -> dict[str, str]:
    """Read a simple KEY=VALUE file (the systemd EnvironmentFile format)."""
    values: dict[str, str] = {}
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    return values


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    lowered = value.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"Expected true or false, got '{value}'.")


def _int(name: str, value: str | None, default: int, minimum: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, got '{value}'.") from None
    if number < minimum:
        raise ConfigError(f"{name} must be {minimum} or more, got {number}.")
    return number


@dataclass
class Settings:
    data_dir: Path = Path("/var/lib/furnace")
    edl_dir: Path = Path("/var/lib/furnace/edl")
    archive_dir: Path = Path("/var/lib/furnace/archive")
    auth_dir: Path = Path("/var/lib/furnace/auth")  # the EDL users file
    backup_dir: Path = Path("/var/lib/furnace/backups")
    archive_after_days: int = 90
    allow_test_ranges: bool = False
    max_ips: int = 50000          # 0 means no limit
    max_domains: int = 50000      # 0 means no limit
    max_urls: int = 50000         # 0 means no limit
    backup_keep: int = 14
    cleanup_max_percent: int = 20
    cleanup_min_size: int = 100
    role_file: Path | None = None  # written by keepalived; None means "always leader"
    selftest_flag: Path | None = None  # root-owned file: test ranges and a setup token for a short time
    lock_timeout: int = 30
    lockout_failures: int = 10    # this many wrong tokens from one IP ...
    lockout_minutes: int = 15     # ... within this time lock that IP for this time
    replica_secret_file: Path | None = None  # HTTPS replication on; the secret both servers share
    # Names that may never be whitelisted, besides the Public Suffix List (one per line). Missing = none.
    whitelist_refuse_file: Path | None = Path("/etc/furnace/whitelist-refuse.txt")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "furnace.db"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "furnace.lock"

    @property
    def blocklist_path(self) -> Path:
        return self.edl_dir / "blocklist.txt"

    @property
    def domains_path(self) -> Path:
        return self.edl_dir / "domains.txt"

    @property
    def urls_path(self) -> Path:
        return self.edl_dir / "urls.txt"

    @property
    def archive_csv_path(self) -> Path:
        return self.archive_dir / "archive.csv"

    @property
    def incoming_dir(self) -> Path:
        return self.data_dir / "incoming"  # snapshots received over HTTPS

    @property
    def edl_users_path(self) -> Path:
        return self.auth_dir / "edl-users"

    @property
    def join_dir(self) -> Path:
        return self.data_dir / "join"  # the waiting join code and the encrypted bundle (furnace.join)

    @property
    def lockout_db_path(self) -> Path:
        return self.data_dir / "lockout.db"

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Settings":
        env = dict(os.environ if environ is None else environ)
        data_dir = Path(env.get("FURNACE_DATA_DIR") or "/var/lib/furnace")
        role_file = env.get("FURNACE_ROLE_FILE") or None
        return cls(
            data_dir=data_dir,
            edl_dir=Path(env.get("FURNACE_EDL_DIR") or data_dir / "edl"),
            archive_dir=Path(env.get("FURNACE_ARCHIVE_DIR") or data_dir / "archive"),
            auth_dir=Path(env.get("FURNACE_AUTH_DIR") or data_dir / "auth"),
            backup_dir=Path(env.get("FURNACE_BACKUP_DIR") or data_dir / "backups"),
            archive_after_days=_int("FURNACE_ARCHIVE_AFTER_DAYS",
                                    env.get("FURNACE_ARCHIVE_AFTER_DAYS"), 90, minimum=1),
            allow_test_ranges=_bool(env.get("FURNACE_ALLOW_TEST_RANGES"), False),
            max_ips=_int("FURNACE_MAX_IPS", env.get("FURNACE_MAX_IPS"), 50000),
            max_domains=_int("FURNACE_MAX_DOMAINS", env.get("FURNACE_MAX_DOMAINS"), 50000),
            max_urls=_int("FURNACE_MAX_URLS", env.get("FURNACE_MAX_URLS"), 50000),
            backup_keep=_int("FURNACE_BACKUP_KEEP", env.get("FURNACE_BACKUP_KEEP"), 14, minimum=1),
            cleanup_max_percent=_int("FURNACE_CLEANUP_MAX_PERCENT",
                                     env.get("FURNACE_CLEANUP_MAX_PERCENT"), 20, minimum=1),
            cleanup_min_size=_int("FURNACE_CLEANUP_MIN_SIZE",
                                  env.get("FURNACE_CLEANUP_MIN_SIZE"), 100),
            role_file=Path(role_file) if role_file else None,
            selftest_flag=Path(env["FURNACE_SELFTEST_FLAG"]) if env.get("FURNACE_SELFTEST_FLAG") else None,
            lock_timeout=_int("FURNACE_LOCK_TIMEOUT", env.get("FURNACE_LOCK_TIMEOUT"), 30, minimum=1),
            lockout_failures=_int("FURNACE_LOCKOUT_FAILURES", env.get("FURNACE_LOCKOUT_FAILURES"), 10, minimum=1),
            lockout_minutes=_int("FURNACE_LOCKOUT_MINUTES", env.get("FURNACE_LOCKOUT_MINUTES"), 15, minimum=1),
            replica_secret_file=(Path(env["FURNACE_REPLICA_SECRET_FILE"])
                                 if env.get("FURNACE_REPLICA_SECRET_FILE") else None),
            whitelist_refuse_file=Path(env.get("FURNACE_WHITELIST_REFUSE_FILE") or "/etc/furnace/whitelist-refuse.txt"),
        )

    @classmethod
    def load(cls, config_file: str | None = None) -> "Settings":
        """Read the config file (if it exists), then let real env vars win."""
        path = config_file or os.environ.get("FURNACE_CONFIG") or DEFAULT_CONFIG_FILE
        env: dict[str, str] = {}
        if os.path.isfile(path):
            env.update(read_env_file(path))
        env.update({k: v for k, v in os.environ.items() if k.startswith("FURNACE_")})
        return cls.from_env(env)

    def is_leader(self) -> bool:
        """True when this server may accept changes.

        keepalived writes 'leader' or 'follower' into the role file.
        With no role file configured (one server, or tests) we are the leader.
        A configured but missing or unreadable file means follower: safe default.
        """
        if self.role_file is None:
            return True
        try:
            return self.role_file.read_text(encoding="utf-8").strip() == "leader"
        except OSError:
            return False

    def _selftest_flag_valid(self) -> bool:
        """Only a recent flag owned by root counts, so a forgotten flag stops working after 10 minutes."""
        if self.selftest_flag is None:
            return False
        try:
            info = self.selftest_flag.stat()
        except OSError:
            return False
        return info.st_uid == 0 and (time.time() - info.st_mtime) < SELFTEST_FLAG_MAX_AGE

    def test_ranges_allowed(self) -> bool:
        """Test ranges are allowed by the setting, or for a short time by the self-test flag.

        The install self-test creates the flag instead of restarting the API, because a
        restart can make keepalived move the shared IP.
        """
        return self.allow_test_ranges or self._selftest_flag_valid()

    def selftest_token(self) -> str:
        """The setup self-test writes a random token into the flag. It works only while the flag is valid."""
        if not self._selftest_flag_valid():
            return ""
        try:
            token = self.selftest_flag.read_text(encoding="utf-8").strip()  # type: ignore[union-attr]
        except OSError:
            return ""
        return token if len(token) >= MIN_SELFTEST_TOKEN_LENGTH else ""

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.edl_dir, self.archive_dir, self.backup_dir, self.auth_dir):
            path.mkdir(parents=True, exist_ok=True)
