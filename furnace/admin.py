"""Data commands for the server admin, run through the furnace command.

The furnace command runs them as the furnace user, for example:
    sudo furnace stats
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path
from typing import Iterable

from . import __version__, auth, backup, db, edl, psl
from .cleanup import run_cleanup
from .config import ConfigError, Settings, read_env_file
from .importer import ImportError_, import_domains, import_ips, import_urls, import_whitelist
from .locking import LockTimeout
from .replica import ReplicaError, apply_snapshot, send_snapshot
from .service import Furnace

EXPORTS = {
    "ips": ("SELECT ip, added_at, added_by, reason,"
            " CASE permanent WHEN 1 THEN 'yes' ELSE 'no' END AS permanent FROM blocked_ips", "ip"),
    "archive": ("SELECT 'ip' AS type, ip AS value, first_blocked_at, last_blocked_at, times_blocked,"
                " CASE WHEN EXISTS(SELECT 1 FROM blocked_ips b WHERE b.ip = r.ip) THEN 'yes' ELSE 'no' END"
                " AS blocked_now, added_by, reason FROM ip_records r UNION ALL"
                " SELECT 'domain', domain, first_blocked_at, last_blocked_at, times_blocked,"
                " CASE WHEN EXISTS(SELECT 1 FROM blocked_domains b WHERE b.domain = r.domain) THEN 'yes' ELSE 'no'"
                " END, added_by, reason FROM domain_records r UNION ALL"
                " SELECT 'url', url, first_blocked_at, last_blocked_at, times_blocked,"
                " CASE WHEN EXISTS(SELECT 1 FROM blocked_urls b WHERE b.url = r.url) THEN 'yes' ELSE 'no'"
                " END, added_by, reason FROM url_records r", "type, value"),
    "domains": ("SELECT domain, added_at, added_by, reason FROM blocked_domains", "domain"),
    "urls": ("SELECT url, added_at, added_by, reason FROM blocked_urls", "url"),
    "whitelist-ips": ("SELECT cidr, added_at, added_by, reason FROM whitelist_ips", "cidr"),
    "whitelist-domains": ("SELECT domain, added_at, added_by, reason FROM whitelist_domains", "domain"),
    "audit": ("SELECT id, ts, action, object_type, value, result, reason_code, actor, source,"
              " details FROM audit_log", "id"),
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="furnace", description="Admin commands for the Furnace blocklist server.")
    p.add_argument("--config", help="Settings file (default /etc/furnace/furnace.env).")
    p.add_argument("--version", action="version", version=f"furnace {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def common_import(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("file", type=Path)
        sp.add_argument("--dry-run", action="store_true", help="Check the file and write the report. Change nothing.")
        sp.add_argument("--report", type=Path, help="Where to write the report of skipped lines.")
        sp.add_argument("--added-by", default="import", help="Name stored as 'added by' (default: import).")
        sp.add_argument("--reason", default="imported", help="Reason for lines that have none.")

    sp = sub.add_parser("import-ips", help="Import IPs from a file (ip / ip,date / ip,date,reason).")
    common_import(sp)
    sp.add_argument("--date-format", help="strftime format of the dates, for example %%d/%%m/%%Y.")
    sp.add_argument("--old-ips", choices=("archive", "fresh"), default="archive",
                    help="IPs with a date older than ARCHIVE_AFTER_DAYS: 'archive' (default) or 'fresh'.")
    sp.add_argument("--permanent", action="store_true",
                    help="Block every IP of the file permanently (they never expire). Old dates are kept.")

    sp = sub.add_parser("import-domains", help="Import domains from a file (domain / domain,reason).")
    common_import(sp)
    sp = sub.add_parser("import-urls", help="Import URLs from a file (url / url reason). http:// and https:// "
                                            "are removed. URLs never expire.")
    common_import(sp)
    sp = sub.add_parser("import-whitelist-ips", help="Import whitelist IPs and ranges from a file.")
    common_import(sp)
    sp = sub.add_parser("import-whitelist-domains", help="Import whitelist domains from a file.")
    common_import(sp)

    sp = sub.add_parser("export", help="Export a list to CSV.")
    sp.add_argument("list", choices=sorted(EXPORTS))
    sp.add_argument("-o", "--output", type=Path, help="Output file (default: print to the screen).")

    sp = sub.add_parser("backup", help="Back up the database and keep the newest N copies.")
    sp.add_argument("--keep", type=int, help="How many copies to keep (default FURNACE_BACKUP_KEEP).")

    sub.add_parser("stats", help="Show counts, dates and file sizes.")
    sub.add_parser("rebuild", help="Rebuild blocklist.txt, domains.txt, urls.txt and the archive CSV from the database.")

    sp = sub.add_parser("cleanup", help="Remove temporary IPs older than ARCHIVE_AFTER_DAYS from the list "
                                        "(they stay in the archive).")
    sp.add_argument("--dry-run", action="store_true", help="Show what would move. Change nothing.")
    sp.add_argument("--force", action="store_true", help="Run even if the safety stop would trigger.")

    sp = sub.add_parser("snapshot", help="(replication) Write a consistent copy of the database.")
    sp.add_argument("output", type=Path)
    sp = sub.add_parser("apply-replica", help="(replication, follower only) Load a snapshot from the leader.")
    sp.add_argument("file", type=Path)
    sp = sub.add_parser("send-replica", help="(replication, leader only) Send a snapshot to the other server (HTTPS).")
    sp.add_argument("peer_ip")
    sp.add_argument("--cafile", type=Path, default=Path("/etc/furnace/tls/furnace.crt"))
    sp.add_argument("--secret-file", type=Path, default=Path("/etc/furnace/secrets/replica-secret"))

    sp = sub.add_parser("token", help="API tokens: list, add NAME, edit NAME, rotate NAME, remove NAME.")
    tok = sp.add_subparsers(dest="action", required=True, metavar="ACTION")
    tok.add_parser("list", help="Show the tokens: permissions, expiry, last use (never the tokens).")
    allow_help = ("Permissions, separated by commas: blocklist:read, blocklist:write, whitelist:read, "
                  "whitelist:write, or all. Write includes read.")
    days_help = f"Days until the token expires, 0 to {auth.MAX_EXPIRES_DAYS} (0 = never)."
    t = tok.add_parser("add", help="Make a new token. Without options it asks. The token is shown once.")
    t.add_argument("name")
    t.add_argument("--allow", help=allow_help)
    t.add_argument("--scope", dest="allow", help=argparse.SUPPRESS)  # the option of version 0.5
    t.add_argument("--expires-days", help=days_help)
    t = tok.add_parser("edit", help="Change a token's permissions or expiry. The token value stays.")
    t.add_argument("name")
    t.add_argument("--allow", help=allow_help)
    t.add_argument("--expires-days", help=days_help + " Counted from now.")
    t = tok.add_parser("rotate", help="Give a token a new value. The old value stops working at once.")
    t.add_argument("name")
    t.add_argument("--expires-days", help=days_help + " Default: the same number of days as before.")
    t = tok.add_parser("remove", help="Delete a token. It stops working at once.")
    t.add_argument("name")

    sp = sub.add_parser("edl-user", help="EDL logins: list, add NAME, rotate NAME, remove NAME.")
    usr = sp.add_subparsers(dest="action", required=True, metavar="ACTION")
    usr.add_parser("list", help="Show the EDL logins (names only).")
    for action, text in (("add", "Make a login with a new password. It is shown once."),
                         ("rotate", "Give a login a new password. The old one stops working at once."),
                         ("remove", "Delete a login.")):
        t = usr.add_parser(action, help=text)
        t.add_argument("name")

    sub.add_parser("lockouts", help="Show the IPs that are locked after too many wrong tokens.")
    sp = sub.add_parser("unlock", help="Unlock one IP, or all with --all.")
    sp.add_argument("ip", nargs="?")
    sp.add_argument("--all", action="store_true")

    sp = sub.add_parser("init-secrets", help="(setup) Make the first token and the EDL login, if there are none.")
    sp.add_argument("--legacy-tokens", type=Path, help="tokens.env from version 0.2: keep these tokens working.")
    sp.add_argument("--token-name", default="api")
    sp.add_argument("--allow", default="all")
    sp.add_argument("--expires-days", default="0")

    sub.add_parser("check-config", help="Check the settings file and show the settings (no secrets).")
    return p


def _actor() -> str:
    return os.environ.get("FURNACE_ACTOR") or "admin"


def _need_leader(settings: Settings) -> None:
    if not settings.is_leader():
        raise auth.AuthError("This server is the follower. Run this on the leader (the server with the shared IP). "
                             "The follower gets the change within 20 seconds.")


def _show_secret(what: str, value: str) -> None:
    print(f"{what} It is shown only once. Store it now:\n\n    {value}\n")


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _input(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        raise auth.AuthError("No answer (the input was closed).") from None


def ask_permissions(current: Iterable[str] = auth.PERMISSIONS) -> tuple[str, ...]:
    """The permission grid. A number switches a box. Write ticks read; no read clears write."""
    boxes = set(current)
    while True:
        print("\n  What may this token do? Type a box number to switch it (or several: 2 4).\n")
        print("                   Read       Write")
        for i, list_name in enumerate(auth.LISTS):
            read, write = (f"[{'x' if f'{list_name}:{a}' in boxes else ' '}]" for a in ("read", "write"))
            print(f"    {list_name.capitalize():<12}  {2 * i + 1} {read}    {2 * i + 2} {write}")
        answer = _input("\n  Box number, or Enter when done: ")
        if not answer:
            if boxes:
                return auth.parse_permissions(boxes)
            print("    Tick at least one box.")
            continue
        numbers = answer.replace(",", " ").split()
        if not all(n in ("1", "2", "3", "4") for n in numbers):
            print("    Type 1, 2, 3 or 4, or press Enter when done.")
            continue
        for n in numbers:
            perm = auth.PERMISSIONS[int(n) - 1]
            list_name, action = perm.split(":")
            if perm in boxes:
                boxes.discard(perm)
                if action == "read":
                    boxes.discard(f"{list_name}:write")
            else:
                boxes.add(perm)
                if action == "write":
                    boxes.add(f"{list_name}:read")


def ask_expires_days(keep: int | None = None) -> int | None:
    """Days until the token expires. With keep, Enter keeps the current expiry (returns None)."""
    hint = f", Enter = keep {keep}" if keep is not None else ""
    while True:
        answer = _input(f"\n  Expires after how many days? (0 = never{hint}): ")
        if not answer and keep is not None:
            return None
        try:
            return auth.check_expires_days(answer)
        except auth.AuthError as error:
            print(f"    {error}")


def _token_summary(token: dict[str, object]) -> str:
    return (f"Permissions: {auth.describe_permissions(token['permissions'])}. "  # type: ignore[arg-type]
            f"Expires: {token['expires']}.")


def print_tokens(conn: sqlite3.Connection) -> None:
    rows = auth.list_tokens(conn)
    if not rows:
        print("There are no API tokens. Make one with: sudo furnace token add NAME")
        return
    def cell(perms: tuple[str, ...], list_name: str) -> str:  # noqa: E306
        actions = [a for a in ("read", "write") if f"{list_name}:{a}" in perms]
        return "+".join(actions) or "-"
    print(f"{'NAME':<24} {'BLOCKLIST':<11} {'WHITELIST':<11} {'EXPIRES':<26} {'LAST USED':<17} ID")
    for r in rows:
        used = str(r["last_used_at"])[:16].replace("T", " ") or "never"
        print(f"{r['name']:<24} {cell(r['permissions'], 'blocklist'):<11} "  # type: ignore[arg-type]
              f"{cell(r['permissions'], 'whitelist'):<11} {r['expires']:<26} {used:<17} {r['id']}")  # type: ignore[arg-type]


def cmd_token(settings: Settings, args: argparse.Namespace) -> int:
    with Furnace(settings).locked() as conn:
        if args.action == "list":
            print_tokens(conn)
            return 0
        _need_leader(settings)
        name = auth.check_name(args.name)
        if args.action == "add":
            allow, days = args.allow, args.expires_days
            if (allow is None or days is None) and not _interactive():
                raise auth.AuthError("Give --allow and --expires-days, or run this in a terminal to be asked.")
            if conn.execute("SELECT 1 FROM api_tokens WHERE name = ?", (name,)).fetchone():
                raise auth.AuthError(f"A token named '{name}' exists already. Use 'token rotate {name}' for a new one.")
            if allow is None:
                allow = ask_permissions()
            if days is None:
                days = ask_expires_days()
            value = auth.add_token(conn, name, allow, _actor(), expires_days=days)
            _show_secret(f"New API token '{name}'. {_token_summary(auth.get_token(conn, name))}", value)
        elif args.action == "edit":
            allow, days = args.allow, args.expires_days
            if allow is None and days is None:
                if not _interactive():
                    raise auth.AuthError("Nothing to change. Give --allow and/or --expires-days.")
                current = auth.get_token(conn, name)
                print(f"Token '{name}'. {_token_summary(current)}")
                allow = ask_permissions(current["permissions"])  # type: ignore[arg-type]
                days = ask_expires_days(keep=int(current["expires_days"]))  # type: ignore[call-overload]
            auth.edit_token(conn, name, _actor(), allow, days)
            print(f"The API token '{name}' is changed. {_token_summary(auth.get_token(conn, name))}")
        elif args.action == "rotate":
            value = auth.rotate_token(conn, name, _actor(), args.expires_days)
            _show_secret(f"New value for the API token '{name}'. The old value no longer works. "
                         f"{_token_summary(auth.get_token(conn, name))}", value)
        else:
            auth.remove_token(conn, name, _actor())
            print(f"The API token '{name}' is deleted. It no longer works.")
        return 0


def token_warnings(conn: sqlite3.Connection) -> list[str]:
    """One line per token that has expired or expires within 14 days."""
    lines = []
    for t in auth.expiring_tokens(conn):
        what = "has expired" if t["expired"] else "expires soon"
        lines.append(f"WARNING: the API token '{t['name']}' {what}: {t['expires']}. "
                     f"Make a new value with: sudo furnace token rotate {t['name']}")
    return lines


PERMANENT_WARN_PERCENT = 50


def permanent_warnings(settings: Settings, permanent: int) -> list[str]:
    """Permanent IPs never leave the list. Warn when they fill half of the IP limit."""
    if not settings.max_ips or 100 * permanent < PERMANENT_WARN_PERCENT * settings.max_ips:
        return []
    return [f"WARNING: {permanent} permanent IPs use {100 * permanent // settings.max_ips} percent of the limit of "
            f"{settings.max_ips}. They never expire. Unblock the ones that are no longer needed."]


def whitelist_warnings(settings: Settings, conn: sqlite3.Connection) -> list[str]:
    """Whitelist domains from before 0.8 that are shared platforms or public names. They are kept (nothing is
    removed on its own), but they protect every site under them, so the admin should replace them."""
    lines = []
    for (domain,) in conn.execute("SELECT domain FROM whitelist_domains ORDER BY domain"):
        why = psl.refusal(domain, settings.whitelist_refuse_file)
        if why:
            lines.append(f"WARNING: the whitelist entry {domain} would not be allowed today: {why} "
                         f"Remove it with the API (DELETE /api/v1/whitelist/domains/{domain}) and whitelist the exact sites.")
    return lines


def cmd_edl_user(settings: Settings, args: argparse.Namespace) -> int:
    with Furnace(settings).locked() as conn:
        if args.action == "list":
            rows = auth.list_edl_users(conn)
            if not rows:
                print("There are no EDL logins, so nobody can download the EDL. "
                      "Make one with: sudo furnace edl-user add NAME")
            for r in rows:
                print(f"{r['name']:<24} made {r['created_at']} by {r['created_by']}")
            return 0
        _need_leader(settings)
        name = auth.check_name(args.name)
        if args.action == "add":
            _show_secret(f"New EDL login '{name}'. Password:", auth.add_edl_user(conn, name, _actor()))
        elif args.action == "rotate":
            _show_secret(f"New password for the EDL login '{name}'. Give it to the device that downloads the lists. "
                         "The old one no longer works.", auth.rotate_edl_user(conn, name, _actor()))
        else:
            auth.remove_edl_user(conn, name, _actor())
            print(f"The EDL login '{name}' is deleted.")
        edl.write_edl_users(conn, settings)
        return 0


def cmd_lockouts(settings: Settings, args: argparse.Namespace) -> int:
    if args.command == "unlock":
        if not args.all and not args.ip:
            print("Give an IP, or --all.", file=sys.stderr)
            return 2
        count = auth.unlock(settings, None if args.all else args.ip)
        print(f"Unlocked {count} address(es).")
        return 0
    rows = auth.list_lockouts(settings)
    if not rows:
        print("No address is locked.")
    for r in rows:
        print(f"{r['ip']:<40} {r['wrong_tokens']} wrong tokens, locked for {r['seconds_left']} more seconds")
    return 0


def cmd_init_secrets(settings: Settings, args: argparse.Namespace) -> int:
    """For setup on the leader. Prints one tab-separated line per new secret:
    token NAME DESCRIPTION VALUE / edl NAME PASSWORD / migrated NAME."""
    with Furnace(settings).locked() as conn:
        if args.legacy_tokens and args.legacy_tokens.is_file():
            for name in auth.import_legacy_tokens(conn, read_env_file(args.legacy_tokens)):
                print(f"migrated\t{name}")
        if not conn.execute("SELECT COUNT(*) FROM api_tokens").fetchone()[0]:
            name = auth.check_name(args.token_name)
            value = auth.add_token(conn, name, args.allow, "setup", expires_days=args.expires_days)
            print(f"token\t{name}\t{_token_summary(auth.get_token(conn, name))}\t{value}")
        if not conn.execute("SELECT COUNT(*) FROM edl_users").fetchone()[0]:
            print(f"edl\tlists\t{auth.add_edl_user(conn, 'lists', 'setup')}")
        edl.write_edl_users(conn, settings)
    return 0


def cmd_import(settings: Settings, args: argparse.Namespace) -> int:
    if not args.file.is_file():
        print(f"The file {args.file} does not exist.", file=sys.stderr)
        return 2
    if args.command == "import-ips":
        report = import_ips(settings, args.file, args.dry_run, args.date_format, args.old_ips,
                            args.added_by, args.reason, args.report, permanent=args.permanent)
    elif args.command == "import-domains":
        report = import_domains(settings, args.file, args.dry_run, args.added_by, args.reason, args.report)
    elif args.command == "import-urls":
        report = import_urls(settings, args.file, args.dry_run, args.added_by, args.reason, args.report)
    else:
        kind = "ips" if args.command == "import-whitelist-ips" else "domains"
        report = import_whitelist(settings, args.file, kind, args.dry_run, args.added_by, args.reason, args.report)
    print(report.summary())
    return 0


def cmd_export(settings: Settings, args: argparse.Namespace) -> int:
    sql, order = EXPORTS[args.list]
    conn = db.connect(settings.db_path)
    try:
        cursor = conn.execute(f"{sql} ORDER BY {order}")
        columns = [c[0] for c in cursor.description]
        handle = open(args.output, "w", newline="", encoding="utf-8") if args.output else sys.stdout
        try:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(columns)
            count = 0
            for row in cursor:
                writer.writerow(list(row))
                count += 1
        finally:
            if args.output:
                handle.close()
    finally:
        conn.close()
    if args.output:
        print(f"Exported {count} rows to {args.output}.")
    return 0


def cmd_stats(settings: Settings) -> int:
    conn = db.connect(settings.db_path)
    try:
        q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
        now = db.utcnow()
        week = db.to_iso(now - timedelta(days=settings.archive_after_days) + timedelta(days=7))
        permanent = q("SELECT COUNT(*) FROM blocked_ips WHERE permanent = 1")
        stats = {
            "role": "leader" if settings.is_leader() else "follower",
            "active_ips": q("SELECT COUNT(*) FROM blocked_ips"),
            "permanent_ips": permanent,
            "domains": q("SELECT COUNT(*) FROM blocked_domains"),
            "urls": q("SELECT COUNT(*) FROM blocked_urls"),
            "archive_ips": q("SELECT COUNT(*) FROM ip_records"),
            "archive_domains": q("SELECT COUNT(*) FROM domain_records"),
            "archive_urls": q("SELECT COUNT(*) FROM url_records"),
            "whitelist_ips": q("SELECT COUNT(*) FROM whitelist_ips"),
            "whitelist_domains": q("SELECT COUNT(*) FROM whitelist_domains"),
            "ip_limit": settings.max_ips or "none",
            "domain_limit": settings.max_domains or "none",
            "url_limit": settings.max_urls or "none",
            "archive_after_days": settings.archive_after_days,
            "oldest_active_ip": q("SELECT MIN(added_at) FROM blocked_ips"),
            "newest_active_ip": q("SELECT MAX(added_at) FROM blocked_ips"),
            "ips_expiring_in_7_days": q("SELECT COUNT(*) FROM blocked_ips WHERE permanent = 0 AND aging_from < ?",
                                        week),
            "last_cleanup": q("SELECT MAX(ts) FROM audit_log WHERE action='cleanup'"),
            "audit_entries": q("SELECT COUNT(*) FROM audit_log"),
            "api_tokens": q("SELECT COUNT(*) FROM api_tokens"),
            "api_tokens_expiring_or_expired": len(auth.expiring_tokens(conn)),
            "edl_logins": q("SELECT COUNT(*) FROM edl_users"),
            "locked_ips": len(auth.list_lockouts(settings)),
            "blocklist_txt_bytes": settings.blocklist_path.stat().st_size if settings.blocklist_path.exists() else None,
            "domains_txt_bytes": settings.domains_path.stat().st_size if settings.domains_path.exists() else None,
            "urls_txt_bytes": settings.urls_path.stat().st_size if settings.urls_path.exists() else None,
            "backups": len(list(settings.backup_dir.glob("furnace-*.db"))) if settings.backup_dir.exists() else 0,
        }
        warnings = token_warnings(conn) + permanent_warnings(settings, permanent) + whitelist_warnings(settings, conn)
    finally:
        conn.close()
    width = max(len(k) for k in stats)
    for key, value in stats.items():
        print(f"{key.replace('_', ' '):<{width}}  {value if value is not None else '-'}")
    if warnings:
        print("\n" + "\n".join(warnings))
    return 0


def cmd_check_config(settings: Settings) -> int:
    safe = {
        "data_dir": str(settings.data_dir), "edl_dir": str(settings.edl_dir),
        "archive_dir": str(settings.archive_dir), "backup_dir": str(settings.backup_dir),
        "archive_after_days": settings.archive_after_days, "allow_test_ranges": settings.allow_test_ranges,
        "max_ips": settings.max_ips, "max_domains": settings.max_domains, "max_urls": settings.max_urls,
        "backup_keep": settings.backup_keep,
        "cleanup_max_percent": settings.cleanup_max_percent, "cleanup_min_size": settings.cleanup_min_size,
        "role_file": str(settings.role_file) if settings.role_file else None,
        "auth_dir": str(settings.auth_dir),
        "lockout": f"{settings.lockout_failures} wrong tokens in {settings.lockout_minutes} minutes",
    }
    print(json.dumps(safe, indent=2))
    print("The settings look good. API tokens: sudo furnace token list. EDL logins: sudo furnace edl-user list.")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.load(args.config)
        if args.command == "check-config":
            return cmd_check_config(settings)
        furnace = Furnace(settings)
        if args.command == "rebuild":
            furnace.startup()
            print("Rebuilt blocklist.txt, domains.txt, urls.txt and the archive CSV from the database.")
            return 0
        settings.ensure_dirs()
        db.init_db(settings.db_path)
        edl.ensure_files(settings)
        if args.command.startswith("import-"):
            return cmd_import(settings, args)
        if args.command == "export":
            return cmd_export(settings, args)
        if args.command == "backup":
            print(f"Backup written: {backup.make_backup(settings, keep=args.keep)}")
            return 0
        if args.command == "stats":
            return cmd_stats(settings)
        if args.command == "cleanup":
            result = run_cleanup(settings, dry_run=args.dry_run, force=args.force)
            print(result.message)
            conn = db.connect(settings.db_path)
            try:
                permanent = conn.execute("SELECT COUNT(*) FROM blocked_ips WHERE permanent = 1").fetchone()[0]
                for line in (token_warnings(conn) + permanent_warnings(settings, permanent)  # into the journal
                             + whitelist_warnings(settings, conn)):
                    print(line)
            finally:
                conn.close()
            if args.dry_run and result.moved:
                print("\n".join(result.moved[:50]) + ("\n..." if len(result.moved) > 50 else ""))
            return result.exit_code
        if args.command == "snapshot":
            backup.snapshot(settings, args.output)
            return 0
        if args.command == "token":
            return cmd_token(settings, args)
        if args.command == "edl-user":
            return cmd_edl_user(settings, args)
        if args.command in ("lockouts", "unlock"):
            return cmd_lockouts(settings, args)
        if args.command == "init-secrets":
            return cmd_init_secrets(settings, args)
        if args.command == "send-replica":
            print(send_snapshot(settings, args.peer_ip, args.cafile, args.secret_file,
                                settings.data_dir / "replica" / "outgoing.db"))
            return 0
        if args.command == "apply-replica":
            counts = apply_snapshot(settings, args.file)
            print(f"Snapshot applied. {counts['ips']} IPs, {counts['domains']} domains, {counts['urls']} URLs.")
            return 0
    except (ConfigError, ImportError_, ReplicaError, auth.AuthError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    except LockTimeout as error:
        print(f"Error: {error} Another change is running. Try again.", file=sys.stderr)
        return 75
    return 1


if __name__ == "__main__":
    sys.exit(main())
