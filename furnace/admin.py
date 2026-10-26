"""Data commands for the server admin, run through the furnace command.

The furnace command runs them as the furnace user, for example:
    sudo furnace stats
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import timedelta
from pathlib import Path

from . import __version__, backup, db, edl
from .cleanup import run_cleanup
from .config import ConfigError, Settings
from .importer import ImportError_, import_domains, import_ips, import_whitelist
from .locking import LockTimeout
from .replica import ReplicaError, apply_snapshot
from .service import Furnace

EXPORTS = {
    "ips": ("SELECT ip, added_at, added_by, reason, incident_id FROM blocked_ips", "ip"),
    "archive": ("SELECT ip, added_at, archived_at, added_by, reason, incident_id FROM archived_ips", "ip"),
    "domains": ("SELECT domain, added_at, added_by, reason, incident_id FROM blocked_domains", "domain"),
    "whitelist-ips": ("SELECT cidr, added_at, added_by, reason FROM whitelist_ips", "cidr"),
    "whitelist-domains": ("SELECT domain, added_at, added_by, reason FROM whitelist_domains", "domain"),
    "audit": ("SELECT id, ts, action, object_type, value, result, reason_code, actor, incident_id, source,"
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

    sp = sub.add_parser("import-domains", help="Import domains from a file (domain / domain,reason).")
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
    sub.add_parser("rebuild", help="Rebuild blocklist.txt, domains.txt and the archive CSV from the database.")

    sp = sub.add_parser("cleanup", help="Move IPs older than ARCHIVE_AFTER_DAYS to the archive.")
    sp.add_argument("--dry-run", action="store_true", help="Show what would move. Change nothing.")
    sp.add_argument("--force", action="store_true", help="Run even if the safety stop would trigger.")

    sp = sub.add_parser("snapshot", help="(replication) Write a consistent copy of the database.")
    sp.add_argument("output", type=Path)
    sp = sub.add_parser("apply-replica", help="(replication, follower only) Load a snapshot from the leader.")
    sp.add_argument("file", type=Path)

    sub.add_parser("check-config", help="Check the settings file and show the settings (no secrets).")
    return p


def cmd_import(settings: Settings, args: argparse.Namespace) -> int:
    if not args.file.is_file():
        print(f"The file {args.file} does not exist.", file=sys.stderr)
        return 2
    if args.command == "import-ips":
        report = import_ips(settings, args.file, args.dry_run, args.date_format, args.old_ips,
                            args.added_by, args.reason, args.report)
    elif args.command == "import-domains":
        report = import_domains(settings, args.file, args.dry_run, args.added_by, args.reason, args.report)
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
        stats = {
            "role": "leader" if settings.is_leader() else "follower",
            "active_ips": q("SELECT COUNT(*) FROM blocked_ips"),
            "archived_ips": q("SELECT COUNT(*) FROM archived_ips"),
            "domains": q("SELECT COUNT(*) FROM blocked_domains"),
            "whitelist_ips": q("SELECT COUNT(*) FROM whitelist_ips"),
            "whitelist_domains": q("SELECT COUNT(*) FROM whitelist_domains"),
            "ip_limit": settings.max_ips or "none",
            "domain_limit": settings.max_domains or "none",
            "archive_after_days": settings.archive_after_days,
            "oldest_active_ip": q("SELECT MIN(added_at) FROM blocked_ips"),
            "newest_active_ip": q("SELECT MAX(added_at) FROM blocked_ips"),
            "ips_expiring_in_7_days": q("SELECT COUNT(*) FROM blocked_ips WHERE aging_from < ?", week),
            "last_cleanup": q("SELECT MAX(ts) FROM audit_log WHERE action='cleanup'"),
            "audit_entries": q("SELECT COUNT(*) FROM audit_log"),
            "blocklist_txt_bytes": settings.blocklist_path.stat().st_size if settings.blocklist_path.exists() else None,
            "domains_txt_bytes": settings.domains_path.stat().st_size if settings.domains_path.exists() else None,
            "backups": len(list(settings.backup_dir.glob("furnace-*.db"))) if settings.backup_dir.exists() else 0,
        }
    finally:
        conn.close()
    width = max(len(k) for k in stats)
    for key, value in stats.items():
        print(f"{key.replace('_', ' '):<{width}}  {value if value is not None else '-'}")
    return 0


def cmd_check_config(settings: Settings) -> int:
    safe = {
        "data_dir": str(settings.data_dir), "edl_dir": str(settings.edl_dir),
        "archive_dir": str(settings.archive_dir), "backup_dir": str(settings.backup_dir),
        "archive_after_days": settings.archive_after_days, "allow_test_ranges": settings.allow_test_ranges,
        "max_ips": settings.max_ips, "max_domains": settings.max_domains, "backup_keep": settings.backup_keep,
        "cleanup_max_percent": settings.cleanup_max_percent, "cleanup_min_size": settings.cleanup_min_size,
        "role_file": str(settings.role_file) if settings.role_file else None,
        "block_token": "set" if settings.block_token else "MISSING",
        "whitelist_token": "set" if settings.whitelist_token else "not set (block token is used)",
    }
    print(json.dumps(safe, indent=2))
    settings.check_tokens()
    print("The settings look good.")
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
            print("Rebuilt blocklist.txt, domains.txt and the archive CSV from the database.")
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
            if args.dry_run and result.moved:
                print("\n".join(result.moved[:50]) + ("\n..." if len(result.moved) > 50 else ""))
            return result.exit_code
        if args.command == "snapshot":
            backup.snapshot(settings, args.output)
            return 0
        if args.command == "apply-replica":
            counts = apply_snapshot(settings, args.file)
            print(f"Snapshot applied. {counts['ips']} IPs, {counts['domains']} domains.")
            return 0
    except (ConfigError, ImportError_, ReplicaError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    except LockTimeout as error:
        print(f"Error: {error} Another change is running. Try again.", file=sys.stderr)
        return 75
    return 1


if __name__ == "__main__":
    sys.exit(main())
