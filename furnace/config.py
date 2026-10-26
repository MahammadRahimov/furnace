"""Settings for Furnace.

All settings come from environment variables that start with FURNACE_.
On a server they live in /etc/furnace/furnace.env, which systemd loads
for the services and which furnace-admin reads on its own.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_FILE = "/etc/furnace/furnace.env"
MIN_TOKEN_LENGTH = 20


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
    backup_dir: Path = Path("/var/lib/furnace/backups")
    archive_after_days: int = 30
    allow_test_ranges: bool = False
    block_token: str = field(default="", repr=False)
    whitelist_token: str = field(default="", repr=False)
    max_ips: int = 50000          # 0 means no limit
    max_domains: int = 50000      # 0 means no limit
    backup_keep: int = 14
    cleanup_max_percent: int = 20
    cleanup_min_size: int = 100
    role_file: Path | None = None  # written by keepalived; None means "always leader"
    lock_timeout: int = 30

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
    def archive_csv_path(self) -> Path:
        return self.archive_dir / "archive.csv"

    @property
    def effective_whitelist_token(self) -> str:
        return self.whitelist_token or self.block_token

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Settings":
        env = dict(os.environ if environ is None else environ)
        data_dir = Path(env.get("FURNACE_DATA_DIR") or "/var/lib/furnace")
        role_file = env.get("FURNACE_ROLE_FILE") or None
        return cls(
            data_dir=data_dir,
            edl_dir=Path(env.get("FURNACE_EDL_DIR") or data_dir / "edl"),
            archive_dir=Path(env.get("FURNACE_ARCHIVE_DIR") or data_dir / "archive"),
            backup_dir=Path(env.get("FURNACE_BACKUP_DIR") or data_dir / "backups"),
            archive_after_days=_int("FURNACE_ARCHIVE_AFTER_DAYS",
                                    env.get("FURNACE_ARCHIVE_AFTER_DAYS"), 30, minimum=1),
            allow_test_ranges=_bool(env.get("FURNACE_ALLOW_TEST_RANGES"), False),
            block_token=env.get("FURNACE_BLOCK_TOKEN", "").strip(),
            whitelist_token=env.get("FURNACE_WHITELIST_TOKEN", "").strip(),
            max_ips=_int("FURNACE_MAX_IPS", env.get("FURNACE_MAX_IPS"), 50000),
            max_domains=_int("FURNACE_MAX_DOMAINS", env.get("FURNACE_MAX_DOMAINS"), 50000),
            backup_keep=_int("FURNACE_BACKUP_KEEP", env.get("FURNACE_BACKUP_KEEP"), 14, minimum=1),
            cleanup_max_percent=_int("FURNACE_CLEANUP_MAX_PERCENT",
                                     env.get("FURNACE_CLEANUP_MAX_PERCENT"), 20, minimum=1),
            cleanup_min_size=_int("FURNACE_CLEANUP_MIN_SIZE",
                                  env.get("FURNACE_CLEANUP_MIN_SIZE"), 100),
            role_file=Path(role_file) if role_file else None,
            lock_timeout=_int("FURNACE_LOCK_TIMEOUT", env.get("FURNACE_LOCK_TIMEOUT"), 30, minimum=1),
        )

    @classmethod
    def load(cls, config_file: str | None = None) -> "Settings":
        """Read the config file (if it exists), then let real env vars win."""
        path = config_file or os.environ.get("FURNACE_CONFIG") or DEFAULT_CONFIG_FILE
        env: dict[str, str] = {}
        if os.path.isfile(path):
            env.update(read_env_file(path))
        env.update({k: v for k, v in os.environ.items() if k.startswith("FURNACE_")})
        # Tokens live in their own file that only the furnace user can read.
        tokens_file = env.get("FURNACE_TOKENS_FILE")
        if tokens_file and os.access(tokens_file, os.R_OK):
            for key, value in read_env_file(tokens_file).items():
                env.setdefault(key, value)
        return cls.from_env(env)

    def check_tokens(self) -> None:
        """The API must not start without a good block token."""
        if not self.block_token:
            raise ConfigError("FURNACE_BLOCK_TOKEN is empty. The API will not start without it.")
        if len(self.block_token) < MIN_TOKEN_LENGTH:
            raise ConfigError(f"FURNACE_BLOCK_TOKEN must be at least {MIN_TOKEN_LENGTH} characters.")
        if self.whitelist_token and len(self.whitelist_token) < MIN_TOKEN_LENGTH:
            raise ConfigError(f"FURNACE_WHITELIST_TOKEN must be at least {MIN_TOKEN_LENGTH} characters.")

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

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.edl_dir, self.archive_dir, self.backup_dir):
            path.mkdir(parents=True, exist_ok=True)
