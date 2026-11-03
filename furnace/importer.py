"""Import IPs, domains and whitelist entries from text files.

Every import is all or nothing (one transaction), takes a database backup
first (unless it is a dry run), and writes a report of every skipped line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from . import backup, db, edl
from .config import Settings
from .locking import FileLock
from .service import Furnace
from .validation import Rejected, clean_text, normalize_domain, normalize_url, validate_ip

COMMENT_PREFIXES = ("#", ";", "//")
_AMBIGUOUS = re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})(?:[ T](\d{1,2}:\d{2}(?::\d{2})?))?$")

KNOWN_FORMATS = (
    "%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
    "%Y/%m/%d", "%Y/%m/%d %H:%M", "%Y/%m/%d %H:%M:%S",
    "%Y%m%d",
    "%d %b %Y", "%d-%b-%Y", "%b %d %Y", "%d %B %Y", "%B %d %Y", "%b %d, %Y", "%B %d, %Y",
    "%d %b %Y %H:%M:%S", "%d-%b-%Y %H:%M:%S",
)


class ImportError_(Exception):
    """The import cannot go ahead. Nothing was changed."""


@dataclass
class Skipped:
    line_no: int
    text: str
    reason: str


@dataclass
class ImportReport:
    kind: str
    dry_run: bool
    total_lines: int = 0
    added_active: int = 0
    added_archive: int = 0
    restored: int = 0
    skipped: list[Skipped] = field(default_factory=list)
    backup_file: str = ""
    report_file: str = ""
    permanent: bool = False
    furnace_file: bool = False                 # a Furnace export read back (furnace/exchange.py)
    removed_from_blocklist: list = field(default_factory=list)

    def summary(self) -> str:
        head = "DRY RUN - nothing was changed. " if self.dry_run else ""
        if self.furnace_file:
            return self._furnace_summary(head)
        parts = [f"{head}Read {self.total_lines} lines.",
                 f"Added to the active list: {self.added_active}."]
        if self.kind == "ips":
            if self.permanent:
                parts.append("All added IPs are permanent.")
            parts.append(f"Added only to the archive (too old to block): {self.added_archive}.")
        if self.kind in ("ips", "domains", "urls"):
            parts.append(f"Blocked again (already in the archive): {self.restored}.")
        parts.append(f"Skipped: {len(self.skipped)}.")
        if self.backup_file:
            parts.append(f"Backup: {self.backup_file}.")
        if self.report_file:
            parts.append(f"Report: {self.report_file}.")
        return "\n".join(parts)

    def _furnace_summary(self, head: str) -> str:
        parts = [f"{head}Furnace export ({self.kind}). Read {self.total_lines} rows."]
        if self.kind.endswith("-archive"):
            parts.append(f"New in the archive: {self.added_archive}. Updated in the archive: {self.restored}.")
        elif self.kind.startswith("whitelist-"):
            parts.append(f"Added to the whitelist: {self.added_active}.")
            if self.removed_from_blocklist:
                verb = "Would be removed" if self.dry_run else "Removed"
                parts.append(f"{verb} from the blocklist (now whitelisted): {len(self.removed_from_blocklist)}.")
        else:
            parts.append(f"Added to the active list with their own dates: {self.added_active}.")
            if self.kind == "ips":
                parts.append(f"Added only to the archive (their time is over): {self.added_archive}.")
            parts.append(f"Already in the archive: {self.restored}.")
        parts.append(f"Skipped: {len(self.skipped)}.")
        if self.backup_file:
            parts.append(f"Backup: {self.backup_file}.")
        if self.report_file:
            parts.append(f"Report: {self.report_file}.")
        return "\n".join(parts)


# ---------------------------------------------------------------------- line parsing

def data_lines(path: Path) -> Iterator[tuple[int, str]]:
    """Yield (line number, text) for lines that are not blank and not comments."""
    with open(path, encoding="utf-8-sig", errors="replace") as handle:
        for number, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line or line.startswith(COMMENT_PREFIXES):
                continue
            yield number, line


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1].strip()
    return text


def split_fields(line: str) -> list[str]:
    """Split on tab, comma or semicolon (the first one found), else on spaces."""
    for sep in ("\t", ",", ";"):
        if sep in line:
            return [_unquote(p) for p in line.split(sep, 2)]
    return line.split()


def ip_fields(line: str, dates: "DateParser") -> list[str]:
    """Fields for an IP line: ip, date, reason. With space separators the date may
    itself contain a space (2026-01-31 12:00:00) and the reason may have many words."""
    fields = split_fields(line)
    if any(sep in line for sep in ("\t", ",", ";")) or len(fields) <= 2:
        return fields
    try:
        dates.parse(f"{fields[1]} {fields[2]}")
        return [fields[0], f"{fields[1]} {fields[2]}", " ".join(fields[3:])]
    except ValueError:
        return [fields[0], fields[1], " ".join(fields[2:])]


def looks_like_header(first_field: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_ \-#]+", first_field)) and ":" not in first_field


class DateParser:
    def __init__(self, date_format: str | None, now: datetime):
        self.date_format = date_format
        self.now = now
        self.day_first: bool | None = None

    def learn(self, samples: list[str]) -> None:
        """For dates like 03/04/2026 decide day-first or month-first from the whole file."""
        if self.date_format:
            return
        day_first = month_first = False
        for sample in samples:
            match = _AMBIGUOUS.match(sample.strip())
            if not match:
                continue
            a, b = int(match.group(1)), int(match.group(2))
            if a > 12:
                day_first = True
            if b > 12:
                month_first = True
        if day_first and month_first:
            raise ImportError_("The file mixes day-first and month-first dates. Use --date-format.")
        if day_first:
            self.day_first = True
        elif month_first:
            self.day_first = False

    def parse(self, text: str) -> datetime:
        text = text.strip()
        if self.date_format:
            return self._utc(datetime.strptime(text, self.date_format))
        if re.fullmatch(r"\d{9,10}(\.\d+)?", text):
            return datetime.fromtimestamp(float(text), tz=timezone.utc).replace(microsecond=0)
        if re.fullmatch(r"\d{13}", text):
            return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc).replace(microsecond=0)
        iso = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            return self._utc(datetime.fromisoformat(iso))
        except ValueError:
            pass
        for fmt in KNOWN_FORMATS:
            try:
                return self._utc(datetime.strptime(text, fmt))
            except ValueError:
                continue
        match = _AMBIGUOUS.match(text)
        if match:
            a, b, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
            day_first = self.day_first
            if day_first is None:
                if a > 12:
                    day_first = True
                elif b > 12:
                    day_first = False
                elif a == b:
                    day_first = True
                else:
                    raise ValueError("ambiguous_date")
            day, month = (a, b) if day_first else (b, a)
            moment = datetime(year, month, day)
            if match.group(4):
                parts = [int(x) for x in match.group(4).split(":")] + [0]
                moment = moment.replace(hour=parts[0], minute=parts[1], second=parts[2])
            return self._utc(moment)
        raise ValueError("invalid_date")

    @staticmethod
    def _utc(moment: datetime) -> datetime:
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).replace(microsecond=0)


# ---------------------------------------------------------------------- helpers

def _require_leader(settings: Settings) -> None:
    if not settings.is_leader():
        raise ImportError_("This server is the standby. Run imports on the leader.")


def _write_report(report: ImportReport, source: Path, report_path: Path | None, now: datetime,
                  settings: Settings) -> None:
    if report_path is None:
        folder = settings.data_dir / "reports"
        folder.mkdir(parents=True, exist_ok=True)
        report_path = folder / f"{source.name}.{now.strftime('%Y%m%d-%H%M%S')}.report.txt"
    path = report_path
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f"Furnace import report - {db.to_iso(now)}\n")
        handle.write(f"Source file: {source}\n")
        handle.write(report.summary() + "\n\n")
        handle.write("Skipped lines (line number | reason | text):\n")
        for item in report.skipped:
            handle.write(f"{item.line_no} | {item.reason} | {item.text[:200]}\n")
    report.report_file = str(path)


def _furnace_kind(source: Path) -> str | None:
    """The list type of a Furnace export (it starts with '# furnace-export'), or None for other files."""
    from . import exchange
    return exchange.detect(source)


def _furnace_import(settings: Settings, source: Path, kind: str, family: str, dry_run: bool, added_by: str,
                    report_path: Path | None, now: datetime | None) -> ImportReport:
    from . import exchange
    return exchange.import_file(settings, source, kind, family, dry_run=dry_run, added_by=added_by,
                                report_path=report_path, now=now)


def _backup_first(settings: Settings, report: ImportReport) -> None:
    if not report.dry_run:
        report.backup_file = str(backup.make_backup(settings, label="pre-import"))


# ---------------------------------------------------------------------- IP import

def import_ips(settings: Settings, source: Path, dry_run: bool = False, date_format: str | None = None,
               old_ips: str = "archive", added_by: str = "import", reason: str = "imported",
               report_path: Path | None = None, now: datetime | None = None,
               permanent: bool = False) -> ImportReport:
    """permanent: every added IP is permanent. Then the dates are only kept, nothing is too old."""
    kind = _furnace_kind(source)
    if kind:
        options = [name for name, used in (("permanent", permanent), ("old_ips", old_ips != "archive"),
                                           ("date_format", bool(date_format))) if used]
        if options:
            raise ImportError_(f"A Furnace export keeps its own dates and permanent flags. Remove the option "
                               f"{', '.join(options)}. Nothing was changed.")
        return _furnace_import(settings, source, kind, "ips", dry_run, added_by, report_path, now)
    if old_ips not in ("archive", "fresh"):
        raise ImportError_("--old-ips must be 'archive' or 'fresh'.")
    _require_leader(settings)
    now = now or db.utcnow()
    days = settings.archive_after_days
    cutoff = now - timedelta(days=days)
    report = ImportReport("ips", dry_run, permanent=permanent)
    added_by = clean_text(added_by, "added_by", 100)
    default_reason = clean_text(reason, "reason", 500)

    lines = list(data_lines(source))
    dates = DateParser(date_format, now)
    dates.learn([f[1] for f in (split_fields(t) for _, t in lines) if len(f) > 1])
    parsed: list[tuple[int, str, list[str]]] = [(n, t, ip_fields(t, dates)) for n, t in lines]

    with FileLock(settings.lock_path, settings.lock_timeout):
        conn = db.connect(settings.db_path)
        try:
            whitelist = Furnace.whitelist_rangeset(conn)
            active = {row[0] for row in conn.execute("SELECT ip FROM blocked_ips")}
            recorded = {row[0] for row in conn.execute("SELECT ip FROM ip_records")}
            seen: set[str] = set()
            dated_active: list[tuple] = []
            to_archive: list[tuple] = []
            undated: list[tuple[str, int, str]] = []   # ip, version, reason
            first_data = True
            for number, text, fields in parsed:
                report.total_lines += 1
                is_first, first_data = first_data, False
                try:
                    ip = validate_ip(fields[0], settings.test_ranges_allowed())
                except Rejected as error:
                    if is_first and looks_like_header(fields[0]):
                        report.skipped.append(Skipped(number, text, "header"))
                    else:
                        report.skipped.append(Skipped(number, text, error.code))
                    continue
                ip_text = str(ip)
                if ip in whitelist:
                    report.skipped.append(Skipped(number, text, "whitelisted"))
                    continue
                if ip_text in seen:
                    report.skipped.append(Skipped(number, text, "duplicate_in_file"))
                    continue
                if ip_text in active:
                    report.skipped.append(Skipped(number, text, "already_active"))
                    continue
                when: datetime | None = None
                if len(fields) > 1 and fields[1]:
                    try:
                        when = dates.parse(fields[1])
                    except ValueError as error:
                        code = "ambiguous_date" if str(error) == "ambiguous_date" else "invalid_date"
                        report.skipped.append(Skipped(number, text, code))
                        continue
                    if when > now + timedelta(days=1):
                        report.skipped.append(Skipped(number, text, "future_date"))
                        continue
                try:
                    line_reason = clean_text(fields[2], "reason", 500) if len(fields) > 2 and fields[2] \
                        else default_reason
                except Rejected:
                    report.skipped.append(Skipped(number, text, "field_too_long"))
                    continue
                seen.add(ip_text)

                if permanent:
                    stamp = db.to_iso(when or now)
                    dated_active.append((ip_text, ip.version, stamp, stamp, added_by, line_reason, 1))
                elif when is not None and when < cutoff:
                    if old_ips == "fresh":
                        undated.append((ip_text, ip.version, line_reason))
                    elif ip_text in recorded:
                        seen.discard(ip_text)
                        report.skipped.append(Skipped(number, text, "already_archived"))
                    else:
                        stamp = db.to_iso(when)
                        to_archive.append((ip_text, ip.version, stamp, stamp, added_by, line_reason))
                elif when is not None:
                    stamp = db.to_iso(when)
                    dated_active.append((ip_text, ip.version, stamp, stamp, added_by, line_reason, 0))
                else:
                    undated.append((ip_text, ip.version, line_reason))

            # Spread the expiry of undated IPs evenly over the next ARCHIVE_AFTER_DAYS days.
            stamp_now = db.to_iso(now)
            spread: list[tuple] = []
            for index, (ip_text, version, line_reason) in enumerate(undated):
                offset = timedelta(seconds=(index + 1) * days * 86400 / (len(undated) + 1))
                aging = db.to_iso(now - timedelta(days=days) + offset)
                spread.append((ip_text, version, stamp_now, aging, added_by, line_reason, 0))

            new_active = dated_active + spread
            restored = [row[0] for row in new_active if row[0] in recorded]
            report.added_active = len(new_active)
            report.added_archive = len(to_archive)
            report.restored = len(restored)
            final_count = len(active) + len(new_active)
            if settings.max_ips and final_count > settings.max_ips:
                raise ImportError_(f"The import would make the list {final_count} IPs long. "
                                   f"The limit is {settings.max_ips}. Nothing was changed.")
            if not dry_run:
                _backup_first(settings, report)
                with db.transaction(conn):
                    conn.executemany(
                        "INSERT INTO blocked_ips(ip, version, added_at, aging_from, added_by, reason,"
                        " permanent) VALUES (?,?,?,?,?,?,?)", new_active)
                    for row in new_active:
                        Furnace.record_ip(conn, row[0], row[1], row[2], row[4], row[5])
                    conn.executemany(
                        "INSERT INTO ip_records(ip, version, first_blocked_at, last_blocked_at, added_by, reason)"
                        " VALUES (?,?,?,?,?,?)", to_archive)
                    audit_rows = [(stamp_now, "import", "ip", row[0], "added", added_by, "import")
                                  for row in new_active]
                    audit_rows += [(stamp_now, "import", "ip", row[0], "archived", added_by, "import")
                                   for row in to_archive]
                    conn.executemany("INSERT INTO audit_log(ts, action, object_type, value, result, actor, source)"
                                     " VALUES (?,?,?,?,?,?,?)", audit_rows)
                    db.audit(conn, "import", "ip", str(source.name), "done", "", added_by, "", "import",
                             f"active={len(new_active)} archive={len(to_archive)} restored={len(restored)} "
                             f"skipped={len(report.skipped)}", now)
                edl.write_blocklist(conn, settings)
                edl.write_archive_csv(conn, settings)
        finally:
            conn.close()
    _write_report(report, source, report_path, now, settings)
    return report


# ---------------------------------------------------------------------- domain import

def import_domains(settings: Settings, source: Path, dry_run: bool = False, added_by: str = "import",
                   reason: str = "imported", report_path: Path | None = None,
                   now: datetime | None = None) -> ImportReport:
    kind = _furnace_kind(source)
    if kind:
        return _furnace_import(settings, source, kind, "domains", dry_run, added_by, report_path, now)
    _require_leader(settings)
    now = now or db.utcnow()
    report = ImportReport("domains", dry_run)
    added_by = clean_text(added_by, "added_by", 100)
    default_reason = clean_text(reason, "reason", 500)
    with FileLock(settings.lock_path, settings.lock_timeout):
        conn = db.connect(settings.db_path)
        try:
            whitelist = {row[0] for row in conn.execute("SELECT domain FROM whitelist_domains")}
            active = {row[0] for row in conn.execute("SELECT domain FROM blocked_domains")}

            def protected(name: str) -> bool:  # the domain or a parent of it is whitelisted
                labels = name.split(".")
                return any(".".join(labels[i:]) in whitelist for i in range(len(labels) - 1))

            recorded = {row[0] for row in conn.execute("SELECT domain FROM domain_records")}
            rows: list[tuple] = []
            seen: set[str] = set()
            first_data = True
            stamp = db.to_iso(now)
            for number, text in data_lines(source):
                report.total_lines += 1
                fields = split_fields(text)
                is_first, first_data = first_data, False
                try:
                    domain = normalize_domain(fields[0])
                    line_reason = clean_text(fields[1], "reason", 500) if len(fields) > 1 and fields[1] \
                        else default_reason
                except Rejected as error:
                    header = is_first and fields[0].lower() in ("domain", "domains", "name", "fqdn", "host")
                    report.skipped.append(Skipped(number, text, "header" if header else error.code))
                    continue
                if protected(domain):
                    report.skipped.append(Skipped(number, text, "whitelisted"))
                elif domain in seen:
                    report.skipped.append(Skipped(number, text, "duplicate_in_file"))
                elif domain in active:
                    report.skipped.append(Skipped(number, text, "already_active"))
                else:
                    seen.add(domain)
                    rows.append((domain, stamp, added_by, line_reason))
            report.added_active = len(rows)
            report.restored = sum(1 for r in rows if r[0] in recorded)
            final_count = len(active) + len(rows)
            if settings.max_domains and final_count > settings.max_domains:
                raise ImportError_(f"The import would make the list {final_count} domains long. "
                                   f"The limit is {settings.max_domains}. Nothing was changed.")
            if not dry_run:
                _backup_first(settings, report)
                with db.transaction(conn):
                    conn.executemany("INSERT INTO blocked_domains(domain, added_at, added_by, reason)"
                                     " VALUES (?,?,?,?)", rows)
                    for r in rows:
                        Furnace.record_domain(conn, r[0], r[1], r[2], r[3])
                    conn.executemany("INSERT INTO audit_log(ts, action, object_type, value, result, actor, source)"
                                     " VALUES (?,?,?,?,?,?,?)",
                                     [(stamp, "import", "domain", r[0], "added", added_by, "import") for r in rows])
                    db.audit(conn, "import", "domain", source.name, "done", "", added_by, "", "import",
                             f"added={len(rows)} skipped={len(report.skipped)}", now)
                edl.write_domains(conn, settings)
                edl.write_archive_csv(conn, settings)
        finally:
            conn.close()
    _write_report(report, source, report_path, now, settings)
    return report


# ---------------------------------------------------------------------- URL import

URL_HEADERS = ("url", "urls", "link", "address", "entry")


def url_fields(line: str) -> tuple[str, str]:
    """A URL line: the URL, then an optional reason after a tab or a space.
    A URL has no spaces, but it may have commas and semicolons, so those do not split."""
    if "\t" in line:
        url, _, reason = line.partition("\t")
    else:
        url, _, reason = line.partition(" ")
    return _unquote(url), _unquote(reason)


def import_urls(settings: Settings, source: Path, dry_run: bool = False, added_by: str = "import",
                reason: str = "imported", report_path: Path | None = None,
                now: datetime | None = None) -> ImportReport:
    """URLs never expire. http:// and https:// are removed; the whitelist protects by host."""
    kind = _furnace_kind(source)
    if kind:
        return _furnace_import(settings, source, kind, "urls", dry_run, added_by, report_path, now)
    _require_leader(settings)
    now = now or db.utcnow()
    report = ImportReport("urls", dry_run)
    added_by = clean_text(added_by, "added_by", 100)
    default_reason = clean_text(reason, "reason", 500)
    with FileLock(settings.lock_path, settings.lock_timeout):
        conn = db.connect(settings.db_path)
        try:
            whitelist = Furnace.url_whitelist(conn)
            active = {row[0] for row in conn.execute("SELECT url FROM blocked_urls")}
            recorded = {row[0] for row in conn.execute("SELECT url FROM url_records")}
            rows: list[tuple] = []
            seen: set[str] = set()
            first_data = True
            stamp = db.to_iso(now)
            for number, text in data_lines(source):
                report.total_lines += 1
                value, line_reason = url_fields(text)
                is_first, first_data = first_data, False
                try:
                    entry = normalize_url(value, settings.test_ranges_allowed())
                    line_reason = clean_text(line_reason, "reason", 500) if line_reason else default_reason
                except Rejected as error:
                    header = is_first and value.lower() in URL_HEADERS
                    report.skipped.append(Skipped(number, text, "header" if header else error.code))
                    continue
                if whitelist.protects(entry):
                    report.skipped.append(Skipped(number, text, "whitelisted"))
                elif entry.url in seen:
                    report.skipped.append(Skipped(number, text, "duplicate_in_file"))
                elif entry.url in active:
                    report.skipped.append(Skipped(number, text, "already_active"))
                else:
                    seen.add(entry.url)
                    rows.append((entry.url, entry.host, stamp, added_by, line_reason))
            report.added_active = len(rows)
            report.restored = sum(1 for r in rows if r[0] in recorded)
            final_count = len(active) + len(rows)
            if settings.max_urls and final_count > settings.max_urls:
                raise ImportError_(f"The import would make the list {final_count} URLs long. "
                                   f"The limit is {settings.max_urls}. Nothing was changed.")
            if not dry_run:
                _backup_first(settings, report)
                with db.transaction(conn):
                    conn.executemany("INSERT INTO blocked_urls(url, host, added_at, added_by, reason)"
                                     " VALUES (?,?,?,?,?)", rows)
                    for r in rows:
                        Furnace.record_url(conn, r[0], r[2], r[3], r[4])
                    conn.executemany("INSERT INTO audit_log(ts, action, object_type, value, result, actor, source)"
                                     " VALUES (?,?,?,?,?,?,?)",
                                     [(stamp, "import", "url", r[0], "added", added_by, "import") for r in rows])
                    db.audit(conn, "import", "url", source.name, "done", "", added_by, "", "import",
                             f"added={len(rows)} skipped={len(report.skipped)}", now)
                edl.write_urls(conn, settings)
                edl.write_archive_csv(conn, settings)
        finally:
            conn.close()
    _write_report(report, source, report_path, now, settings)
    return report


# ---------------------------------------------------------------------- whitelist import

def import_whitelist(settings: Settings, source: Path, kind: str, dry_run: bool = False,
                     added_by: str = "import", reason: str = "imported", report_path: Path | None = None,
                     now: datetime | None = None) -> ImportReport:
    """kind is 'ips' or 'domains'. Uses the same rules as the bulk API."""
    found = _furnace_kind(source)
    if found:
        return _furnace_import(settings, source, found, f"whitelist-{kind}", dry_run, added_by, report_path, now)
    _require_leader(settings)
    now = now or db.utcnow()
    report = ImportReport(f"whitelist-{kind}", dry_run)
    numbered: list[tuple[int, str]] = []
    first = True
    for number, text in data_lines(source):
        report.total_lines += 1
        value = split_fields(text)[0]
        if first and looks_like_header(value) and value.lower() in (
                "ip", "ips", "cidr", "range", "network", "address", "domain", "domains", "name", "entry"):
            report.skipped.append(Skipped(number, text, "header"))
        else:
            numbered.append((number, value))
        first = False
    if not dry_run:
        _backup_first(settings, report)
    furnace = Furnace(settings)
    bulk = furnace.bulk_whitelist_ips if kind == "ips" else furnace.bulk_whitelist_domains
    result = bulk([v for _, v in numbered], reason, added_by, dry_run=dry_run, source="import", now=now)
    if result.status == "rejected":
        raise ImportError_(result.message)
    by_value: dict[str, list[int]] = {}
    for number, value in numbered:
        by_value.setdefault(value.strip()[:300], []).append(number)
    for item in result.data["rejected"]:
        for number in by_value.get(item["value"], [0]):
            report.skipped.append(Skipped(number, item["value"], item["reason_code"]))
    report.added_active = result.data["added"]
    already = result.data["already_exists"]
    if already:
        report.skipped.append(Skipped(0, f"{already} entries", "already_on_whitelist_or_duplicate"))
    removed = result.data["removed_from_blocklist"] + result.data["urls_removed_from_blocklist"]
    for value in removed:
        report.skipped.append(Skipped(0, value, "removed_from_blocklist" if not dry_run
                                      else "would_be_removed_from_blocklist"))
    report.skipped.sort(key=lambda s: s.line_no)
    _write_report(report, source, report_path, now, settings)
    return report
