"""Furnace export files: write a list to CSV, and read the same file back (user decision 2026-11-03).

An export starts with a marker line, then the CSV header, then one row per entry:

    # furnace-export ips v1
    # Furnace 0.8.0, exported 2026-11-03T10:00:00Z. Upload it again with the bulk API or furnace import-ips.
    ip,added_at,added_by,reason,permanent,counted_from
    203.0.113.5,2026-10-01T10:00:00Z,soar,phishing,no,2026-10-01T10:00:00Z

The importers look for the marker. With it they read every column by name and keep the dates, who added
the entry, the reason, the permanent flag and the archive history, so a list moves to a new server as it
was. Without it a file is read as before (ip, date, reason). Tokens, logins and the certificate are never
exported: they are secrets, and a new server makes its own.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

from . import __version__, backup, db, edl
from .config import Settings
from .locking import FileLock
from .rangeset import RangeSet
from .service import Furnace
from .validation import (Rejected, clean_text, normalize_domain, normalize_url, range_hex, validate_ip,
                         validate_whitelist_cidr)

MARK = "# furnace-export"
FORMAT = "v1"
_ARCHIVE_COLUMNS = ("first_blocked_at", "last_blocked_at", "times_blocked", "blocked_now", "added_by", "reason")


@dataclass(frozen=True)
class Spec:
    kind: str           # the name in the marker line and in GET /api/v1/export/<kind>
    family: str         # the import that reads it: ips, domains, urls, whitelist-ips, whitelist-domains
    permission: str     # blocklist or whitelist (read, for the export)
    columns: tuple
    required: tuple     # the columns an import needs; the others may be missing (empty)
    sql: str
    order: str

    @property
    def key(self) -> str:
        return self.columns[0]

    @property
    def archive(self) -> bool:
        return self.kind.endswith("-archive")


def _archive_sql(table: str, key: str, blocked: str) -> str:
    return (f"SELECT {key}, first_blocked_at, last_blocked_at, times_blocked,"
            f" CASE WHEN EXISTS(SELECT 1 FROM {blocked} b WHERE b.{key} = r.{key}) THEN 'yes' ELSE 'no' END"
            f" AS blocked_now, added_by, reason FROM {table} r")


SPECS = {s.kind: s for s in (
    Spec("ips", "ips", "blocklist",
         ("ip", "added_at", "added_by", "reason", "permanent", "counted_from"), ("ip", "added_at"),
         "SELECT ip, added_at, added_by, reason, CASE permanent WHEN 1 THEN 'yes' ELSE 'no' END,"
         " aging_from FROM blocked_ips", "version, ip"),
    Spec("ips-archive", "ips", "blocklist", ("ip",) + _ARCHIVE_COLUMNS, ("ip", "first_blocked_at", "last_blocked_at"),
         _archive_sql("ip_records", "ip", "blocked_ips"), "version, ip"),
    Spec("domains", "domains", "blocklist", ("domain", "added_at", "added_by", "reason"), ("domain", "added_at"),
         "SELECT domain, added_at, added_by, reason FROM blocked_domains", "domain"),
    Spec("domains-archive", "domains", "blocklist", ("domain",) + _ARCHIVE_COLUMNS,
         ("domain", "first_blocked_at", "last_blocked_at"),
         _archive_sql("domain_records", "domain", "blocked_domains"), "domain"),
    Spec("urls", "urls", "blocklist", ("url", "added_at", "added_by", "reason"), ("url", "added_at"),
         "SELECT url, added_at, added_by, reason FROM blocked_urls", "url"),
    Spec("urls-archive", "urls", "blocklist", ("url",) + _ARCHIVE_COLUMNS,
         ("url", "first_blocked_at", "last_blocked_at"), _archive_sql("url_records", "url", "blocked_urls"), "url"),
    Spec("whitelist-ips", "whitelist-ips", "whitelist", ("cidr", "added_at", "added_by", "reason"),
         ("cidr", "added_at"), "SELECT cidr, added_at, added_by, reason FROM whitelist_ips", "version, start_hex"),
    Spec("whitelist-domains", "whitelist-domains", "whitelist", ("domain", "added_at", "added_by", "reason"),
         ("domain", "added_at"), "SELECT domain, added_at, added_by, reason FROM whitelist_domains", "domain"),
)}

IMPORT_COMMAND = {"ips": "import-ips", "domains": "import-domains", "urls": "import-urls",
                  "whitelist-ips": "import-whitelist-ips", "whitelist-domains": "import-whitelist-domains"}
BULK_PATH = {"ips": "/api/v1/ips/bulk", "domains": "/api/v1/domains/bulk", "urls": "/api/v1/urls/bulk",
             "whitelist-ips": "/api/v1/whitelist/ips/bulk", "whitelist-domains": "/api/v1/whitelist/domains/bulk"}


# ---------------------------------------------------------------------- export

def header_lines(spec: Spec, now: datetime | None = None) -> list[str]:
    stamp = db.to_iso(now or db.utcnow())
    return [f"{MARK} {spec.kind} {FORMAT}",
            f"# Furnace {__version__}, exported {stamp}. Upload it again with {BULK_PATH[spec.family]}"
            f" or furnace {IMPORT_COMMAND[spec.family]}."]


def rows(conn, spec: Spec) -> Iterator[tuple]:
    yield from conn.execute(f"{spec.sql} ORDER BY {spec.order}")


def csv_chunks(conn, spec: Spec, now: datetime | None = None, batch: int = 2000) -> Iterator[str]:
    """The export as CSV text, in pieces (the API streams them)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    for line in header_lines(spec, now):
        buffer.write(line + "\n")
    writer.writerow(spec.columns)
    for count, row in enumerate(rows(conn, spec), 1):
        writer.writerow(list(row))
        if count % batch == 0:
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate()
    yield buffer.getvalue()


# ---------------------------------------------------------------------- reading a file back

def detect(path: Path) -> str | None:
    """The kind of a Furnace export, or None for any other file. The marker is the first line that is not blank."""
    from .importer import ImportError_
    with open(path, encoding="utf-8-sig", errors="replace") as handle:
        for raw in handle:
            line = raw.strip().rstrip(",;\t ").strip('"').strip()  # a spreadsheet may add commas or quotes
            if not line:
                continue
            if not line.startswith(MARK):
                return None
            parts = line[len(MARK):].split()
            if not parts or parts[0] not in SPECS:
                raise ImportError_(f"This Furnace export has an unknown list type ({' '.join(parts) or 'none'}). "
                                   f"Known: {', '.join(SPECS)}. Nothing was changed.")
            if len(parts) < 2 or parts[1] != FORMAT:
                raise ImportError_(f"This Furnace export has the format {parts[1] if len(parts) > 1 else '(none)'}; "
                                   f"this Furnace reads {FORMAT}. Export it again or upgrade Furnace. "
                                   "Nothing was changed.")
            return parts[0]
    return None


def _records(path: Path, spec: Spec) -> Iterator[tuple[int, str, dict[str, str]]]:
    """(line number, text, row by column name) for every data row. Comment and blank lines are skipped."""
    from .importer import ImportError_
    columns: list[str] | None = None
    delimiter = ","
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as handle:
        for number, raw in enumerate(handle, 1):
            text = raw.strip("\r\n")
            if not text.strip() or text.lstrip().lstrip('"').startswith("#"):
                continue
            if columns is None:
                if ";" in text and "," not in text:   # a spreadsheet that saves with semicolons
                    delimiter = ";"
                columns = [c.strip().lower() for c in next(csv.reader([text], delimiter=delimiter))]
                missing = [c for c in spec.required if c not in columns]
                if missing:
                    raise ImportError_(f"The file has no column {', '.join(missing)}. A Furnace {spec.kind} export "
                                       f"has the columns {', '.join(spec.columns)}. Nothing was changed.")
                continue
            cells = next(csv.reader([text], delimiter=delimiter), [])
            row = {name: (cells[i].strip() if i < len(cells) else "") for i, name in enumerate(columns)}
            yield number, text, row
    if columns is None:
        raise ImportError_("The file has the Furnace marker but no header line. Nothing was changed.")


def _time(text: str, now: datetime) -> datetime:
    from .importer import DateParser
    try:
        return db.from_iso(text.strip())
    except ValueError:
        return DateParser(None, now).parse(text)   # a spreadsheet may have changed the form


def _yes_no(text: str) -> bool:
    value = text.strip().lower()
    if value in ("yes", "true", "1"):
        return True
    if value in ("no", "false", "0", ""):
        return False
    raise ValueError("invalid_permanent")


def import_file(settings: Settings, source: Path, kind: str, family: str, dry_run: bool = False,
                added_by: str = "import", report_path: Path | None = None, now: datetime | None = None):
    """Read a Furnace export back. kind comes from detect(); family is the import it was sent to."""
    from .importer import ImportError_, ImportReport, Skipped, _require_leader, _write_report
    spec = SPECS[kind]
    if spec.family != family:
        raise ImportError_(f"This file is a Furnace {kind} export. Upload it with {BULK_PATH[spec.family]} or "
                           f"furnace {IMPORT_COMMAND[spec.family]}. Nothing was changed.")
    _require_leader(settings)
    now = now or db.utcnow()
    actor = clean_text(added_by, "added_by", 100) or "import"
    report = ImportReport(kind, dry_run, furnace_file=True)
    test = settings.test_ranges_allowed()

    def skip(number: int, text: str, code: str) -> None:
        report.skipped.append(Skipped(number, text, code))

    def texts(row: dict[str, str]) -> tuple[str, str]:
        return clean_text(row.get("added_by", ""), "added_by", 100), clean_text(row.get("reason", ""), "reason", 500)

    def times(row: dict[str, str], *names: str) -> list[datetime]:
        out = []
        for name in names:
            moment = _time(row[name], now)
            if moment > now + timedelta(days=1):
                raise ValueError("future_date")
            out.append(moment)
        return out

    with FileLock(settings.lock_path, settings.lock_timeout):
        conn = db.connect(settings.db_path)
        try:
            furnace = Furnace(settings)
            plan = _Plan()
            seen: set[str] = set()
            for number, text, row in _records(source, spec):
                report.total_lines += 1
                value = row.get(spec.key, "")
                try:
                    if spec.archive:
                        key = _archive_key(spec, value, test)
                        first, last = times(row, "first_blocked_at", "last_blocked_at")
                        count = int(row.get("times_blocked") or 1)
                        if count < 1:
                            raise ValueError("invalid_times_blocked")
                        by, reason = texts(row)
                    else:
                        key, extra = _active_key(spec, value, test)
                        (added,) = times(row, "added_at")
                        by, reason = texts(row)
                except Rejected as error:
                    skip(number, text, error.code)
                    continue
                except ValueError as error:
                    code = str(error)
                    skip(number, text, code if code in ("future_date", "ambiguous_date", "invalid_permanent",
                                                        "invalid_times_blocked") else
                         "invalid_times_blocked" if "int" in code else "invalid_date")
                    continue
                if key in seen:
                    skip(number, text, "duplicate_in_file")
                    continue
                seen.add(key)
                if spec.archive:
                    plan.records.append((key, db.to_iso(min(first, last)), db.to_iso(max(first, last)), count, by,
                                         reason))
                    continue
                try:
                    code = _plan_active(conn, furnace, spec, plan, key, extra, added, by, reason, row, now, settings)
                except ValueError as error:
                    code = str(error)
                if code:
                    skip(number, text, code)
            _apply(conn, furnace, spec, plan, report, settings, source, actor, dry_run, now)
        finally:
            conn.close()
    _write_report(report, source, report_path, now, settings)
    return report


class _Plan:
    def __init__(self) -> None:
        self.active: list[tuple] = []      # rows for the active list or the whitelist
        self.records: list[tuple] = []     # archive rows: key, first, last, times, added_by, reason
        self.old: list[tuple] = []         # IPs too old to block: they go to the archive only
        self.cache: dict[str, object] = {}


def _archive_key(spec: Spec, value: str, test: bool) -> str:
    if spec.family == "ips":
        return str(validate_ip(value, allow_test_ranges=test))
    if spec.family == "domains":
        return normalize_domain(value)
    return normalize_url(value, test).url


def _active_key(spec: Spec, value: str, test: bool) -> tuple[str, object]:
    if spec.family == "ips":
        ip = validate_ip(value, allow_test_ranges=test)
        return str(ip), ip
    if spec.family == "domains":
        return normalize_domain(value), None
    if spec.family == "urls":
        entry = normalize_url(value, test)
        return entry.url, entry
    if spec.family == "whitelist-ips":
        net = validate_whitelist_cidr(value)
        return str(net), net
    return normalize_domain(value), None


def _plan_active(conn, furnace: Furnace, spec: Spec, plan: _Plan, key: str, extra: object, added: datetime,
                 by: str, reason: str, row: dict[str, str], now: datetime, settings: Settings) -> str:
    """Add one active (or whitelist) row to the plan. Returns a skip reason, or '' when it is taken."""
    cache = plan.cache
    stamp = db.to_iso(added)
    if spec.family == "ips":
        if "active" not in cache:
            cache["active"] = {r[0] for r in conn.execute("SELECT ip FROM blocked_ips")}
            cache["whitelist"] = Furnace.whitelist_rangeset(conn)
        if extra in cache["whitelist"]:
            return "whitelisted"
        if key in cache["active"]:
            return "already_active"
        permanent = _yes_no(row.get("permanent", ""))
        counted = _time(row["counted_from"], now) if row.get("counted_from") else added
        if not permanent and counted < now - timedelta(days=settings.archive_after_days):
            plan.old.append((key, extra.version, stamp, stamp, 1, by, reason))
            return ""
        plan.active.append((key, extra.version, stamp, db.to_iso(counted), by, reason, 1 if permanent else 0))
        return ""
    if spec.family == "domains":
        if "active" not in cache:
            cache["active"] = {r[0] for r in conn.execute("SELECT domain FROM blocked_domains")}
            cache["whitelist"] = {r[0] for r in conn.execute("SELECT domain FROM whitelist_domains")}
        labels = key.split(".")
        if any(".".join(labels[i:]) in cache["whitelist"] for i in range(len(labels) - 1)):
            return "whitelisted"
        if key in cache["active"]:
            return "already_active"
        plan.active.append((key, stamp, by, reason))
        return ""
    if spec.family == "urls":
        if "active" not in cache:
            cache["active"] = {r[0] for r in conn.execute("SELECT url FROM blocked_urls")}
            cache["whitelist"] = Furnace.url_whitelist(conn)
        if cache["whitelist"].protects(extra):
            return "whitelisted"
        if key in cache["active"]:
            return "already_active"
        plan.active.append((key, extra.host, stamp, by, reason))
        return ""
    if spec.family == "whitelist-ips":
        if "existing" not in cache:
            cache["existing"] = {r[0] for r in conn.execute("SELECT cidr FROM whitelist_ips")}
        if key in cache["existing"]:
            return "already_on_whitelist"
        plan.active.append((key, extra.version, *range_hex(extra), stamp, by, reason))
        return ""
    if "existing" not in cache:
        cache["existing"] = {r[0] for r in conn.execute("SELECT domain FROM whitelist_domains")}
    if key in cache["existing"]:
        return "already_on_whitelist"
    try:
        furnace._whitelist_check(key)
    except Rejected as error:
        return error.code
    plan.active.append((key, stamp, by, reason))
    return ""


_TABLES = {"ips": ("ip_records", "ip"), "domains": ("domain_records", "domain"), "urls": ("url_records", "url")}


def _merge_records(conn, family: str, records: list[tuple], versions: dict[str, int]) -> tuple[int, int]:
    """Write archive rows. A new key is added; an existing one keeps the earliest first block (with its
    added_by and reason), the latest last block and the higher count. Returns (added, updated)."""
    table, key = _TABLES[family]
    added = updated = 0
    for value, first, last, count, by, reason in records:
        old = conn.execute(f"SELECT first_blocked_at, last_blocked_at, times_blocked FROM {table} WHERE {key}=?",
                           (value,)).fetchone()
        if old is None:
            if family == "ips":
                conn.execute("INSERT INTO ip_records(ip, version, first_blocked_at, last_blocked_at, times_blocked,"
                             " added_by, reason) VALUES (?,?,?,?,?,?,?)",
                             (value, versions[value], first, last, count, by, reason))
            else:
                conn.execute(f"INSERT INTO {table}({key}, first_blocked_at, last_blocked_at, times_blocked, added_by,"
                             " reason) VALUES (?,?,?,?,?,?)", (value, first, last, count, by, reason))
            added += 1
            continue
        new = (min(old[0], first), max(old[1], last), max(old[2], count))
        if new == tuple(old):
            continue
        conn.execute(f"UPDATE {table} SET first_blocked_at=?, last_blocked_at=?, times_blocked=? WHERE {key}=?",
                     (*new, value))
        if first < old[0]:
            conn.execute(f"UPDATE {table} SET added_by=?, reason=? WHERE {key}=?", (by, reason, value))
        updated += 1
    return added, updated


def _apply(conn, furnace: Furnace, spec: Spec, plan: _Plan, report, settings: Settings, source: Path, actor: str,
           dry_run: bool, now: datetime) -> None:
    from .importer import ImportError_
    family = spec.family
    stamp_now = db.to_iso(now)
    limits = {"ips": ("blocked_ips", settings.max_ips, "IPs"), "domains": ("blocked_domains", settings.max_domains,
              "domains"), "urls": ("blocked_urls", settings.max_urls, "URLs")}
    if family in limits and plan.active:
        table, limit, word = limits[family]
        final = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] + len(plan.active)
        if limit and final > limit:
            raise ImportError_(f"The import would make the list {final} {word} long. The limit is {limit}. "
                               "Nothing was changed.")
    if family in _TABLES and not spec.archive:
        table, key = _TABLES[family]
        recorded = {r[0] for r in conn.execute(f"SELECT {key} FROM {table}")}
        report.restored = sum(1 for r in plan.active if r[0] in recorded)
    report.added_active = len(plan.active)
    report.added_archive = len(plan.old)
    if dry_run:
        if family == "whitelist-ips" and plan.active:
            ranges = RangeSet(validate_whitelist_cidr(r[0]) for r in plan.active)
            report.removed_from_blocklist = [r[0] for r in conn.execute("SELECT ip FROM blocked_ips")
                                             if r[0] in ranges] + Furnace.urls_covered(conn, ranges=ranges)
        elif family == "whitelist-domains" and plan.active:
            names = sorted({r[0] for r in plan.active})
            report.removed_from_blocklist = Furnace.domains_covered(conn, names) + Furnace.urls_covered(conn, names)
        if spec.archive:
            table, key = _TABLES[family]
            known = {r[0] for r in conn.execute(f"SELECT {key} FROM {table}")}
            report.added_archive = sum(1 for r in plan.records if r[0] not in known)
            report.restored = len(plan.records) - report.added_archive
        return
    report.backup_file = str(backup.make_backup(settings, label="pre-import"))
    removed: list[str] = []
    urls: list[str] = []
    with db.transaction(conn):
        if family == "ips":
            if spec.archive:
                versions = {r[0]: (6 if ":" in r[0] else 4) for r in plan.records}
                report.added_archive, report.restored = _merge_records(conn, "ips", plan.records, versions)
            else:
                conn.executemany("INSERT INTO blocked_ips(ip, version, added_at, aging_from, added_by, reason,"
                                 " permanent) VALUES (?,?,?,?,?,?,?)", plan.active)
                records = [(r[0], r[2], r[2], 1, r[4], r[5]) for r in plan.active]
                records += [(r[0], r[2], r[3], r[4], r[5], r[6]) for r in plan.old]
                versions = {r[0]: r[1] for r in plan.active + plan.old}
                _merge_records(conn, "ips", records, versions)
        elif family in ("domains", "urls"):
            if spec.archive:
                report.added_archive, report.restored = _merge_records(conn, family, plan.records, {})
            elif family == "domains":
                conn.executemany("INSERT INTO blocked_domains(domain, added_at, added_by, reason) VALUES (?,?,?,?)",
                                 plan.active)
                _merge_records(conn, family, [(r[0], r[1], r[1], 1, r[2], r[3]) for r in plan.active], {})
            else:
                conn.executemany("INSERT INTO blocked_urls(url, host, added_at, added_by, reason) VALUES (?,?,?,?,?)",
                                 plan.active)
                _merge_records(conn, family, [(r[0], r[2], r[2], 1, r[3], r[4]) for r in plan.active], {})
        elif family == "whitelist-ips":
            conn.executemany("INSERT INTO whitelist_ips(cidr, version, start_hex, end_hex, added_at, added_by, reason)"
                             " VALUES (?,?,?,?,?,?,?)", plan.active)
            ranges = RangeSet(validate_whitelist_cidr(r[0]) for r in plan.active)
            removed = furnace._purge_ips(conn, ranges, actor, "import", now, "from an imported Furnace file")
            urls = furnace._purge_urls(conn, actor, "import", now, ranges=ranges)
        else:
            conn.executemany("INSERT INTO whitelist_domains(domain, added_at, added_by, reason) VALUES (?,?,?,?)",
                             plan.active)
            names = {r[0] for r in plan.active}
            removed = furnace._purge_domains(conn, names, actor, "import", now)
            urls = furnace._purge_urls(conn, actor, "import", now, domains=sorted(names))
        object_type = {"ips": "ip", "domains": "domain", "urls": "url", "whitelist-ips": "whitelist_ip",
                       "whitelist-domains": "whitelist_domain"}[family]
        if spec.archive:
            audit_rows = [(stamp_now, "import", object_type, r[0], "archive_record", actor, "import")
                          for r in plan.records]
        else:
            audit_rows = [(stamp_now, "import", object_type, r[0], "added", actor, "import") for r in plan.active]
            audit_rows += [(stamp_now, "import", object_type, r[0], "archived", actor, "import") for r in plan.old]
        conn.executemany("INSERT INTO audit_log(ts, action, object_type, value, result, actor, source)"
                         " VALUES (?,?,?,?,?,?,?)", audit_rows)
        db.audit(conn, "import", object_type, source.name, "done", "", actor, "", "import",
                 f"furnace-export {spec.kind}: active={report.added_active} archive={report.added_archive} "
                 f"updated={report.restored} skipped={len(report.skipped)}", now)
    report.removed_from_blocklist = removed + urls
    for which in {"ips": ("ips",), "domains": ("domains",), "urls": ("urls",),
                  "whitelist-ips": ("ips", "urls"), "whitelist-domains": ("domains", "urls")}[family]:
        {"ips": edl.write_blocklist, "domains": edl.write_domains, "urls": edl.write_urls}[which](conn, settings)
    edl.write_archive_csv(conn, settings)
