"""Blocklist and whitelist rules.

Every change follows the same path:
  take the file lock -> one database transaction -> commit -> rebuild the files.
The database is the master record. The text files are always rebuilt from it.
"""

from __future__ import annotations

import ipaddress
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Iterator

from . import db, edl
from .config import Settings
from .locking import FileLock
from .rangeset import RangeSet
from .validation import (Rejected, URLEntry, clean_text, domain_key, host_touches_domain, ip_sort_key,
                         normalize_domain, normalize_url, range_hex, to_hex, validate_ip, validate_whitelist_cidr)

log = logging.getLogger("furnace")

REASON_MAX = 500
NAME_MAX = 100


@dataclass
class Result:
    status: str                      # added, already_exists, removed, rejected, not_found, found, ok
    message: str
    code: str = ""                   # reason code when rejected
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status, "message": self.message}
        if self.code:
            out["reason_code"] = self.code
        out.update(self.data)
        return out


class URLWhitelist:
    """The whitelist as the URL list sees it: loaded once, then many URLs are checked fast."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.by_key: dict[str, list[str]] = {}
        for (domain,) in conn.execute("SELECT domain FROM whitelist_domains"):
            self.by_key.setdefault(domain_key(domain), []).append(domain)
        for names in self.by_key.values():
            names.sort(key=len)

    def protects(self, entry: URLEntry) -> str | None:
        """The whitelist entry that protects this URL, or None."""
        if entry.is_ip:
            return Furnace._ip_whitelisted(self.conn, ipaddress.ip_address(entry.host))
        for domain in self.by_key.get(domain_key(entry.host), ()):
            if host_touches_domain(entry.host, domain):
                return domain
        return None


class Furnace:
    def __init__(self, settings: Settings):
        self.settings = settings

    # ------------------------------------------------------------------ plumbing

    def startup(self) -> None:
        """Create folders, the database and empty EDL files, then rebuild from the database."""
        self.settings.ensure_dirs()
        db.init_db(self.settings.db_path)
        edl.ensure_files(self.settings)
        with self.locked() as conn:
            edl.rebuild_all(conn, self.settings)

    @contextmanager
    def locked(self) -> Iterator[sqlite3.Connection]:
        with FileLock(self.settings.lock_path, self.settings.lock_timeout):
            conn = db.connect(self.settings.db_path)
            try:
                yield conn
            finally:
                conn.close()

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        conn = db.connect(self.settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def _rebuild(self, conn: sqlite3.Connection, *which: str) -> None:
        """Rebuild files after a commit. A failure here never undoes the change:
        the next change, the nightly job or 'furnace rebuild' writes the file again."""
        writers = {"ips": edl.write_blocklist, "domains": edl.write_domains, "urls": edl.write_urls,
                   "archive": edl.write_archive_csv}
        for name in which:
            try:
                writers[name](conn, self.settings)
            except Exception:
                log.exception("Could not rebuild the %s file. Run 'furnace rebuild'.", name)

    def _not_leader(self, action: str, object_type: str, value: str) -> Result | None:
        if self.settings.is_leader():
            return None
        log.warning("Refused %s %s %s: this server is the standby.", action, object_type, value)
        return Result("rejected", Rejected("not_leader").message, code="not_leader")

    def _reject(self, conn: sqlite3.Connection, action: str, object_type: str, value: str,
                error: Rejected, actor: str, source: str,
                now: datetime | None) -> Result:
        with db.transaction(conn):
            db.audit(conn, action, object_type, value, "rejected", error.code, actor, "",
                     source, error.message, now)
        return Result("rejected", error.message, code=error.code)

    @staticmethod
    def _texts(reason: object, added_by: object) -> tuple[str, str]:
        return (clean_text(reason, "reason", REASON_MAX),
                clean_text(added_by, "added_by", NAME_MAX))

    @staticmethod
    def _raw(value: object) -> str:
        return value.strip()[:300] if isinstance(value, str) else repr(value)[:300]

    @staticmethod
    def _permanent(value: object) -> bool:
        if value is None:
            return False
        if not isinstance(value, bool):
            raise Rejected("invalid_permanent")
        return value

    # ------------------------------------------------------------------ the archive record

    @staticmethod
    def record_ip(conn: sqlite3.Connection, ip: str, version: int, stamp: str, actor: str, reason: str) -> bool:
        """Write the archive record of a block. Returns True if the IP was blocked before.
        Caller holds the transaction."""
        if conn.execute("UPDATE ip_records SET last_blocked_at = MAX(last_blocked_at, ?),"
                        " times_blocked = times_blocked + 1 WHERE ip = ?", (stamp, ip)).rowcount:
            return True
        conn.execute("INSERT INTO ip_records(ip, version, first_blocked_at, last_blocked_at, added_by, reason)"
                     " VALUES (?,?,?,?,?,?)", (ip, version, stamp, stamp, actor, reason))
        return False

    @staticmethod
    def record_domain(conn: sqlite3.Connection, domain: str, stamp: str, actor: str, reason: str) -> bool:
        if conn.execute("UPDATE domain_records SET last_blocked_at = MAX(last_blocked_at, ?),"
                        " times_blocked = times_blocked + 1 WHERE domain = ?", (stamp, domain)).rowcount:
            return True
        conn.execute("INSERT INTO domain_records(domain, first_blocked_at, last_blocked_at, added_by, reason)"
                     " VALUES (?,?,?,?,?)", (domain, stamp, stamp, actor, reason))
        return False

    @staticmethod
    def _record(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {k: row[k] for k in ("first_blocked_at", "last_blocked_at", "times_blocked", "added_by", "reason")}

    # ------------------------------------------------------------------ whitelist lookups

    @staticmethod
    def _ip_whitelisted(conn: sqlite3.Connection, ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
        key = to_hex(ip)
        row = conn.execute(
            "SELECT cidr FROM whitelist_ips WHERE version=? AND start_hex<=? AND end_hex>=? LIMIT 1",
            (ip.version, key, key)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _domain_whitelisted(conn: sqlite3.Connection, domain: str) -> bool:
        return conn.execute("SELECT 1 FROM whitelist_domains WHERE domain=?", (domain,)).fetchone() is not None

    @staticmethod
    def whitelist_rangeset(conn: sqlite3.Connection) -> RangeSet:
        return RangeSet(ipaddress.ip_network(row[0]) for row in conn.execute("SELECT cidr FROM whitelist_ips"))

    # ------------------------------------------------------------------ IPs

    def add_ip(self, value: object, reason: object = "", added_by: object = "",
               source: str = "api", now: datetime | None = None, permanent: object = False) -> Result:
        raw = self._raw(value)
        refused = self._not_leader("add", "ip", raw)
        if refused:
            return refused
        now = now or db.utcnow()
        with self.locked() as conn:
            try:
                reason_t, actor = self._texts(reason, added_by)
                forever = self._permanent(permanent)
            except Rejected as error:
                return self._reject(conn, "add", "ip", raw, error, "", source, now)
            try:
                ip = validate_ip(value, self.settings.test_ranges_allowed())
                cidr = self._ip_whitelisted(conn, ip)
                if cidr:
                    raise Rejected("whitelisted", f"This IP is covered by the whitelist entry {cidr}. "
                                                  "It cannot be blocked.")
            except Rejected as error:
                return self._reject(conn, "add", "ip", raw, error, actor, source, now)

            text = str(ip)
            stamp = db.to_iso(now)
            with db.transaction(conn):
                existing = conn.execute("SELECT * FROM blocked_ips WHERE ip=?", (text,)).fetchone()
                if existing:
                    db.audit(conn, "add", "ip", text, "already_exists", "", actor, "", source,
                             f"Already blocked since {existing['added_at']}.", now)
                    record = conn.execute("SELECT * FROM ip_records WHERE ip=?", (text,)).fetchone()
                    return Result("already_exists",
                                  f"This IP is already blocked since {existing['added_at']}. Nothing changed.",
                                  data={"entry": self._ip_entry(existing), "record": self._record(record)})
                if self.settings.max_ips:
                    count = conn.execute("SELECT COUNT(*) FROM blocked_ips").fetchone()[0]
                    if count >= self.settings.max_ips:
                        db.audit(conn, "add", "ip", text, "rejected", "limit_reached", actor, "",
                                 source, f"Limit is {self.settings.max_ips}.", now)
                        return Result("rejected", Rejected("limit_reached").message, code="limit_reached")
                conn.execute(
                    "INSERT INTO blocked_ips(ip, version, added_at, aging_from, added_by, reason, permanent)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (text, ip.version, stamp, stamp, actor, reason_t, int(forever)))
                before = self.record_ip(conn, text, ip.version, stamp, actor, reason_t)
                db.audit(conn, "add", "ip", text, "restored" if before else "added", "", actor, "", source,
                         ("Blocked before (in the archive). " if before else "")
                         + ("Permanent." if forever else ""), now)
                record = conn.execute("SELECT * FROM ip_records WHERE ip=?", (text,)).fetchone()
                row = conn.execute("SELECT * FROM blocked_ips WHERE ip=?", (text,)).fetchone()
            self._rebuild(conn, "ips")
            data = {"entry": self._ip_entry(row), "record": self._record(record)}
            kind = "permanently" if forever else f"for {self.settings.archive_after_days} days"
            if before:
                data["restored_from_archive"] = True
                return Result("added", f"This IP was blocked before (it is in the archive). "
                                       f"It is blocked again {kind}, with a new date.", data=data)
            return Result("added", f"The IP is now blocked {kind}.", data=data)

    def remove_ip(self, value: object, removed_by: object = "", source: str = "api",
                  now: datetime | None = None) -> Result:
        raw = self._raw(value)
        refused = self._not_leader("remove", "ip", raw)
        if refused:
            return refused
        now = now or db.utcnow()
        with self.locked() as conn:
            try:
                actor = clean_text(removed_by, "removed_by", NAME_MAX)
                ip = validate_ip(value, allow_test_ranges=True)
            except Rejected as error:
                return self._reject(conn, "remove", "ip", raw, error, "", source, now)
            text = str(ip)
            with db.transaction(conn):
                gone = conn.execute("DELETE FROM blocked_ips WHERE ip=?", (text,)).rowcount
                db.audit(conn, "remove", "ip", text, "removed" if gone else "not_found", "", actor, "",
                         source, "", now)
            if not gone:
                return Result("not_found", "This IP is not in the active blocklist.")
            self._rebuild(conn, "ips")
            return Result("removed", "The IP is no longer blocked.", data={"ip": text})

    def get_ip(self, value: object) -> Result:
        try:
            ip = validate_ip(value, allow_test_ranges=True)
        except Rejected as error:
            return Result("rejected", error.message, code=error.code)
        text = str(ip)
        with self._reader() as conn:
            cidr = self._ip_whitelisted(conn, ip)
            row = conn.execute("SELECT * FROM blocked_ips WHERE ip=?", (text,)).fetchone()
            record = self._record(conn.execute("SELECT * FROM ip_records WHERE ip=?", (text,)).fetchone())
        if row:
            return Result("found", "This IP is blocked.",
                          data={"list": "active", "entry": self._ip_entry(row), "record": record,
                                "whitelisted_by": cidr})
        if record:
            return Result("found", "This IP is in the archive: it was blocked before. It is not blocked now.",
                          data={"list": "archive", "entry": {"ip": text, **record}, "record": record,
                                "whitelisted_by": cidr})
        return Result("not_found", "This IP was never blocked.", data={"whitelisted_by": cidr})

    def _ip_entry(self, row: sqlite3.Row) -> dict[str, Any]:
        entry: dict[str, Any] = {k: row[k] for k in ("ip", "added_at", "added_by", "reason")}
        entry["permanent"] = bool(row["permanent"])
        entry["expires_at"] = None if row["permanent"] else db.to_iso(
            db.from_iso(row["aging_from"]) + timedelta(days=self.settings.archive_after_days))
        return entry

    # ------------------------------------------------------------------ domains

    def add_domain(self, value: object, reason: object = "", added_by: object = "",
                   source: str = "api", now: datetime | None = None) -> Result:
        raw = self._raw(value)
        refused = self._not_leader("add", "domain", raw)
        if refused:
            return refused
        now = now or db.utcnow()
        with self.locked() as conn:
            try:
                reason_t, actor = self._texts(reason, added_by)
            except Rejected as error:
                return self._reject(conn, "add", "domain", raw, error, "", source, now)
            try:
                domain = normalize_domain(value)
                if self._domain_whitelisted(conn, domain):
                    raise Rejected("whitelisted")
            except Rejected as error:
                return self._reject(conn, "add", "domain", raw, error, actor, source, now)
            stamp = db.to_iso(now)
            with db.transaction(conn):
                existing = conn.execute("SELECT * FROM blocked_domains WHERE domain=?", (domain,)).fetchone()
                if existing:
                    db.audit(conn, "add", "domain", domain, "already_exists", "", actor, "", source,
                             f"Already blocked since {existing['added_at']}.", now)
                    record = conn.execute("SELECT * FROM domain_records WHERE domain=?", (domain,)).fetchone()
                    return Result("already_exists",
                                  f"This domain is already blocked since {existing['added_at']}. Nothing changed.",
                                  data={"entry": {k: existing[k] for k in ("domain", "added_at", "added_by",
                                                                           "reason")},
                                        "record": self._record(record)})
                if self.settings.max_domains:
                    count = conn.execute("SELECT COUNT(*) FROM blocked_domains").fetchone()[0]
                    if count >= self.settings.max_domains:
                        db.audit(conn, "add", "domain", domain, "rejected", "limit_reached", actor, "",
                                 source, f"Limit is {self.settings.max_domains}.", now)
                        return Result("rejected", Rejected("limit_reached").message, code="limit_reached")
                conn.execute("INSERT INTO blocked_domains(domain, added_at, added_by, reason)"
                             " VALUES (?,?,?,?)", (domain, stamp, actor, reason_t))
                before = self.record_domain(conn, domain, stamp, actor, reason_t)
                db.audit(conn, "add", "domain", domain, "restored" if before else "added", "", actor, "",
                         source, "Blocked before (in the archive)." if before else "", now)
                record = conn.execute("SELECT * FROM domain_records WHERE domain=?", (domain,)).fetchone()
            self._rebuild(conn, "domains")
            data: dict[str, Any] = {"entry": {"domain": domain, "added_at": stamp, "added_by": actor,
                                              "reason": reason_t},
                                    "record": self._record(record)}
            if before:
                data["restored_from_archive"] = True
                return Result("added", "This domain was blocked before (it is in the archive). "
                                       "It is blocked again.", data=data)
            return Result("added", "The domain is now blocked.", data=data)

    def remove_domain(self, value: object, removed_by: object = "", source: str = "api",
                      now: datetime | None = None) -> Result:
        raw = self._raw(value)
        refused = self._not_leader("remove", "domain", raw)
        if refused:
            return refused
        now = now or db.utcnow()
        with self.locked() as conn:
            try:
                actor = clean_text(removed_by, "removed_by", NAME_MAX)
                domain = normalize_domain(value)
            except Rejected as error:
                return self._reject(conn, "remove", "domain", raw, error, "", source, now)
            with db.transaction(conn):
                gone = conn.execute("DELETE FROM blocked_domains WHERE domain=?", (domain,)).rowcount
                db.audit(conn, "remove", "domain", domain, "removed" if gone else "not_found", "", actor,
                         "", source, "", now)
            if not gone:
                return Result("not_found", "This domain is not in the blocklist.")
            self._rebuild(conn, "domains")
            return Result("removed", "The domain is no longer blocked.", data={"domain": domain})

    def get_domain(self, value: object) -> Result:
        try:
            domain = normalize_domain(value)
        except Rejected as error:
            return Result("rejected", error.message, code=error.code)
        with self._reader() as conn:
            whitelisted = self._domain_whitelisted(conn, domain)
            row = conn.execute("SELECT * FROM blocked_domains WHERE domain=?", (domain,)).fetchone()
            record = self._record(conn.execute("SELECT * FROM domain_records WHERE domain=?",
                                               (domain,)).fetchone())
        if row:
            return Result("found", "This domain is blocked.",
                          data={"list": "active",
                                "entry": {k: row[k] for k in ("domain", "added_at", "added_by", "reason")},
                                "record": record, "whitelisted": whitelisted})
        if record:
            return Result("found", "This domain is in the archive: it was blocked before. It is not blocked now.",
                          data={"list": "archive", "entry": {"domain": domain, **record}, "record": record,
                                "whitelisted": whitelisted})
        return Result("not_found", "This domain was never blocked.", data={"whitelisted": whitelisted})

    # ------------------------------------------------------------------ URLs
    #
    # URLs never expire. The whitelist protects them by host (user decision 2026-11-02):
    # a domain on the domain whitelist protects itself and all its subdomains, and an IP host
    # is checked against the IP whitelist.

    @staticmethod
    def record_url(conn: sqlite3.Connection, url: str, stamp: str, actor: str, reason: str) -> bool:
        if conn.execute("UPDATE url_records SET last_blocked_at = MAX(last_blocked_at, ?),"
                        " times_blocked = times_blocked + 1 WHERE url = ?", (stamp, url)).rowcount:
            return True
        conn.execute("INSERT INTO url_records(url, first_blocked_at, last_blocked_at, added_by, reason)"
                     " VALUES (?,?,?,?,?)", (url, stamp, stamp, actor, reason))
        return False

    @staticmethod
    def url_whitelist(conn: sqlite3.Connection) -> "URLWhitelist":
        return URLWhitelist(conn)

    def add_url(self, value: object, reason: object = "", added_by: object = "",
                source: str = "api", now: datetime | None = None) -> Result:
        raw = self._raw(value)
        refused = self._not_leader("add", "url", raw)
        if refused:
            return refused
        now = now or db.utcnow()
        with self.locked() as conn:
            try:
                reason_t, actor = self._texts(reason, added_by)
            except Rejected as error:
                return self._reject(conn, "add", "url", raw, error, "", source, now)
            try:
                entry = normalize_url(value, self.settings.test_ranges_allowed())
                protected = self.url_whitelist(conn).protects(entry)
                if protected:
                    raise Rejected("whitelisted", f"This URL is covered by the whitelist entry {protected}. "
                                                  "It cannot be blocked.")
            except Rejected as error:
                return self._reject(conn, "add", "url", raw, error, actor, source, now)
            url, stamp = entry.url, db.to_iso(now)
            with db.transaction(conn):
                existing = conn.execute("SELECT * FROM blocked_urls WHERE url=?", (url,)).fetchone()
                if existing:
                    db.audit(conn, "add", "url", url, "already_exists", "", actor, "", source,
                             f"Already blocked since {existing['added_at']}.", now)
                    record = conn.execute("SELECT * FROM url_records WHERE url=?", (url,)).fetchone()
                    return Result("already_exists",
                                  f"This URL is already blocked since {existing['added_at']}. Nothing changed.",
                                  data={"entry": self._url_entry(existing), "record": self._record(record)})
                if self.settings.max_urls:
                    count = conn.execute("SELECT COUNT(*) FROM blocked_urls").fetchone()[0]
                    if count >= self.settings.max_urls:
                        db.audit(conn, "add", "url", url, "rejected", "limit_reached", actor, "",
                                 source, f"Limit is {self.settings.max_urls}.", now)
                        return Result("rejected", Rejected("limit_reached").message, code="limit_reached")
                conn.execute("INSERT INTO blocked_urls(url, host, added_at, added_by, reason) VALUES (?,?,?,?,?)",
                             (url, entry.host, stamp, actor, reason_t))
                before = self.record_url(conn, url, stamp, actor, reason_t)
                db.audit(conn, "add", "url", url, "restored" if before else "added", "", actor, "",
                         source, "Blocked before (in the archive)." if before else "", now)
                record = conn.execute("SELECT * FROM url_records WHERE url=?", (url,)).fetchone()
                row = conn.execute("SELECT * FROM blocked_urls WHERE url=?", (url,)).fetchone()
            self._rebuild(conn, "urls")
            data: dict[str, Any] = {"entry": self._url_entry(row), "record": self._record(record)}
            if raw.lower() != url:
                data["given"] = raw
            if before:
                data["restored_from_archive"] = True
                return Result("added", f"This URL was blocked before (it is in the archive). "
                                       f"It is blocked again as {url}.", data=data)
            return Result("added", f"The URL is now blocked as {url}.", data=data)

    def remove_url(self, value: object, removed_by: object = "", source: str = "api",
                   now: datetime | None = None) -> Result:
        raw = self._raw(value)
        refused = self._not_leader("remove", "url", raw)
        if refused:
            return refused
        now = now or db.utcnow()
        with self.locked() as conn:
            try:
                actor = clean_text(removed_by, "removed_by", NAME_MAX)
                url = normalize_url(value, allow_test_ranges=True).url
            except Rejected as error:
                return self._reject(conn, "remove", "url", raw, error, "", source, now)
            with db.transaction(conn):
                gone = conn.execute("DELETE FROM blocked_urls WHERE url=?", (url,)).rowcount
                db.audit(conn, "remove", "url", url, "removed" if gone else "not_found", "", actor,
                         "", source, "", now)
            if not gone:
                return Result("not_found", "This URL is not in the blocklist.", data={"url": url})
            self._rebuild(conn, "urls")
            return Result("removed", "The URL is no longer blocked.", data={"url": url})

    def get_url(self, value: object) -> Result:
        try:
            entry = normalize_url(value, allow_test_ranges=True)
        except Rejected as error:
            return Result("rejected", error.message, code=error.code)
        with self._reader() as conn:
            protected = self.url_whitelist(conn).protects(entry)
            row = conn.execute("SELECT * FROM blocked_urls WHERE url=?", (entry.url,)).fetchone()
            record = self._record(conn.execute("SELECT * FROM url_records WHERE url=?", (entry.url,)).fetchone())
        if row:
            return Result("found", "This URL is blocked.",
                          data={"list": "active", "entry": self._url_entry(row), "record": record,
                                "whitelisted_by": protected})
        if record:
            return Result("found", "This URL is in the archive: it was blocked before. It is not blocked now.",
                          data={"list": "archive", "entry": {"url": entry.url, **record}, "record": record,
                                "whitelisted_by": protected})
        return Result("not_found", "This URL was never blocked.", data={"url": entry.url, "whitelisted_by": protected})

    @staticmethod
    def _url_entry(row: sqlite3.Row) -> dict[str, Any]:
        return {k: row[k] for k in ("url", "added_at", "added_by", "reason")}

    def _purge_urls(self, conn: sqlite3.Connection, actor: str, source: str, now: datetime,
                    domains: Iterable[str] = (), ranges: RangeSet | None = None) -> list[str]:
        """Remove blocked URLs that new whitelist entries protect. Caller holds the transaction."""
        hit = self.urls_covered(conn, domains, ranges)
        for url in hit:
            conn.execute("DELETE FROM blocked_urls WHERE url=?", (url,))
            db.audit(conn, "remove", "url", url, "removed_by_whitelist", "", actor, "", source, "Whitelisted.", now)
        return hit

    @staticmethod
    def urls_covered(conn: sqlite3.Connection, domains: Iterable[str] = (),
                     ranges: RangeSet | None = None) -> list[str]:
        """The blocked URLs that these (new) whitelist domains or IP ranges protect."""
        by_key: dict[str, list[str]] = {}
        for d in domains:
            by_key.setdefault(domain_key(d), []).append(d)
        if not by_key and not (ranges and len(ranges)):
            return []
        hit = []
        for url, host in conn.execute("SELECT url, host FROM blocked_urls").fetchall():
            if URLEntry(url, host).is_ip:
                covered = ranges is not None and host in ranges
            else:
                covered = any(host_touches_domain(host, d) for d in by_key.get(domain_key(host), ()))
            if covered:
                hit.append(url)
        return sorted(hit)

    # ------------------------------------------------------------------ IP whitelist

    def _purge_ips(self, conn: sqlite3.Connection, ranges: RangeSet, actor: str, source: str,
                   now: datetime, cause: str) -> list[str]:
        """Remove blocked IPs covered by whitelist ranges. Caller holds the transaction."""
        if not len(ranges):
            return []
        hit = [row[0] for row in conn.execute("SELECT ip FROM blocked_ips") if row[0] in ranges]
        for ip in hit:
            conn.execute("DELETE FROM blocked_ips WHERE ip=?", (ip,))
            db.audit(conn, "remove", "ip", ip, "removed_by_whitelist", "", actor, "", source,
                     f"Covered by whitelist entry {cause}.", now)
        hit.sort(key=ip_sort_key)
        return hit

    def add_whitelist_ip(self, value: object, reason: object = "", added_by: object = "",
                         source: str = "api", now: datetime | None = None) -> Result:
        raw = self._raw(value)
        refused = self._not_leader("add", "whitelist_ip", raw)
        if refused:
            return refused
        now = now or db.utcnow()
        with self.locked() as conn:
            try:
                reason_t, actor = self._texts(reason, added_by)
                net = validate_whitelist_cidr(value)
            except Rejected as error:
                return self._reject(conn, "add", "whitelist_ip", raw, error, "", source, now)
            cidr = str(net)
            with db.transaction(conn):
                if conn.execute("SELECT 1 FROM whitelist_ips WHERE cidr=?", (cidr,)).fetchone():
                    db.audit(conn, "add", "whitelist_ip", cidr, "already_exists", "", actor, "", source, "", now)
                    return Result("already_exists", "This entry is already on the whitelist. Nothing changed.",
                                  data={"cidr": cidr})
                start, end = range_hex(net)
                conn.execute("INSERT INTO whitelist_ips(cidr, version, start_hex, end_hex, added_at, added_by,"
                             " reason) VALUES (?,?,?,?,?,?,?)",
                             (cidr, net.version, start, end, db.to_iso(now), actor, reason_t))
                db.audit(conn, "add", "whitelist_ip", cidr, "added", "", actor, "", source, "", now)
                removed = self._purge_ips(conn, RangeSet([net]), actor, source, now, cidr)
                urls = self._purge_urls(conn, actor, source, now, ranges=RangeSet([net]))
            self._rebuild_after_whitelist(conn, "ips", removed, urls)
            return Result("added", self._wl_message(removed, "IP", urls),
                          data={"cidr": cidr, "removed_from_blocklist": removed, "urls_removed_from_blocklist": urls})

    @staticmethod
    def _wl_message(removed: list[str], kind: str, urls: list[str] = ()) -> str:  # type: ignore[assignment]
        text = "The entry is on the whitelist now."
        for count, what in ((len(removed), kind), (len(urls), "URL")):
            if count:
                text += f" {count} blocked {what}{'s were' if count != 1 else ' was'} removed from the blocklist."
        return text

    def _rebuild_after_whitelist(self, conn: sqlite3.Connection, kind: str, removed: list[str],
                                 urls: list[str]) -> None:
        self._rebuild(conn, *([kind] if removed else []), *(["urls"] if urls else []))

    def remove_whitelist_ip(self, value: object, removed_by: object = "", source: str = "api",
                            now: datetime | None = None) -> Result:
        raw = self._raw(value)
        refused = self._not_leader("remove", "whitelist_ip", raw)
        if refused:
            return refused
        now = now or db.utcnow()
        with self.locked() as conn:
            try:
                actor = clean_text(removed_by, "removed_by", NAME_MAX)
                cidr = str(validate_whitelist_cidr(value))
            except Rejected as error:
                return self._reject(conn, "remove", "whitelist_ip", raw, error, "", source, now)
            with db.transaction(conn):
                gone = conn.execute("DELETE FROM whitelist_ips WHERE cidr=?", (cidr,)).rowcount
                db.audit(conn, "remove", "whitelist_ip", cidr, "removed" if gone else "not_found", "",
                         actor, "", source, "", now)
        if not gone:
            return Result("not_found", "This entry is not on the whitelist.")
        return Result("removed", "The entry is no longer on the whitelist.", data={"cidr": cidr})

    def list_whitelist_ips(self, limit: int = 1000, offset: int = 0) -> Result:
        with self._reader() as conn:
            total = conn.execute("SELECT COUNT(*) FROM whitelist_ips").fetchone()[0]
            rows = conn.execute("SELECT cidr, added_at, added_by, reason FROM whitelist_ips"
                                " ORDER BY version, start_hex LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return Result("ok", f"{total} entries on the IP whitelist.",
                      data={"total": total, "limit": limit, "offset": offset, "entries": [dict(r) for r in rows]})

    def bulk_whitelist_ips(self, values: Iterable[object], reason: object = "", added_by: object = "",
                           dry_run: bool = False, source: str = "api", now: datetime | None = None) -> Result:
        values = list(values)
        refused = None if dry_run else self._not_leader("bulk_add", "whitelist_ip", f"{len(values)} entries")
        if refused:
            return refused
        now = now or db.utcnow()
        try:
            reason_t, actor = self._texts(reason, added_by)
        except Rejected as error:
            return Result("rejected", error.message, code=error.code)
        rejected: list[dict[str, str]] = []
        nets: dict[str, ipaddress.IPv4Network | ipaddress.IPv6Network] = {}
        for value in values:
            try:
                net = validate_whitelist_cidr(value)
                nets.setdefault(str(net), net)
            except Rejected as error:
                rejected.append({"value": self._raw(value), "reason_code": error.code, "message": error.message})

        with (self._reader() if dry_run else self.locked()) as conn:
            existing = {row[0] for row in conn.execute("SELECT cidr FROM whitelist_ips")}
            new = {c: n for c, n in nets.items() if c not in existing}
            ranges = RangeSet(new.values())
            if dry_run:
                covered = sorted((row[0] for row in conn.execute("SELECT ip FROM blocked_ips")
                                  if row[0] in ranges), key=ip_sort_key)
                removed = covered
                urls = self.urls_covered(conn, ranges=ranges)
            else:
                stamp = db.to_iso(now)
                with db.transaction(conn):
                    conn.executemany(
                        "INSERT INTO whitelist_ips(cidr, version, start_hex, end_hex, added_at, added_by, reason)"
                        " VALUES (?,?,?,?,?,?,?)",
                        [(c, n.version, *range_hex(n), stamp, actor, reason_t) for c, n in new.items()])
                    for c in new:
                        db.audit(conn, "add", "whitelist_ip", c, "added", "", actor, "", source, "bulk", now)
                    for item in rejected:
                        db.audit(conn, "add", "whitelist_ip", item["value"], "rejected", item["reason_code"],
                                 actor, "", source, "bulk", now)
                    removed = self._purge_ips(conn, ranges, actor, source, now, "from bulk upload")
                    urls = self._purge_urls(conn, actor, source, now, ranges=ranges)
                self._rebuild_after_whitelist(conn, "ips", removed, urls)
        summary = {"dry_run": dry_run, "received": len(values), "added": len(new),
                   "already_exists": len(nets) - len(new), "rejected": rejected,
                   "removed_from_blocklist": removed, "urls_removed_from_blocklist": urls}
        verb = "would be" if dry_run else "were"
        return Result("ok", f"{len(new)} entries {verb} added. {len(rejected)} entries {verb} rejected. "
                            f"{len(removed)} blocked IPs and {len(urls)} blocked URLs {verb} removed.", data=summary)

    # ------------------------------------------------------------------ domain whitelist

    def add_whitelist_domain(self, value: object, reason: object = "", added_by: object = "",
                             source: str = "api", now: datetime | None = None) -> Result:
        raw = self._raw(value)
        refused = self._not_leader("add", "whitelist_domain", raw)
        if refused:
            return refused
        now = now or db.utcnow()
        with self.locked() as conn:
            try:
                reason_t, actor = self._texts(reason, added_by)
                domain = normalize_domain(value)
            except Rejected as error:
                return self._reject(conn, "add", "whitelist_domain", raw, error, "", source, now)
            with db.transaction(conn):
                if self._domain_whitelisted(conn, domain):
                    db.audit(conn, "add", "whitelist_domain", domain, "already_exists", "", actor, "", source, "", now)
                    return Result("already_exists", "This domain is already on the whitelist. Nothing changed.",
                                  data={"domain": domain})
                conn.execute("INSERT INTO whitelist_domains(domain, added_at, added_by, reason) VALUES (?,?,?,?)",
                             (domain, db.to_iso(now), actor, reason_t))
                db.audit(conn, "add", "whitelist_domain", domain, "added", "", actor, "", source, "", now)
                removed = self._purge_domains(conn, {domain}, actor, source, now)
                urls = self._purge_urls(conn, actor, source, now, domains=[domain])
            self._rebuild_after_whitelist(conn, "domains", removed, urls)
            return Result("added", self._wl_message(removed, "domain", urls),
                          data={"domain": domain, "removed_from_blocklist": removed,
                                "urls_removed_from_blocklist": urls})

    def _purge_domains(self, conn: sqlite3.Connection, domains: set[str], actor: str, source: str,
                       now: datetime) -> list[str]:
        hit = sorted(row[0] for row in conn.execute("SELECT domain FROM blocked_domains") if row[0] in domains)
        for domain in hit:
            conn.execute("DELETE FROM blocked_domains WHERE domain=?", (domain,))
            db.audit(conn, "remove", "domain", domain, "removed_by_whitelist", "", actor, "", source,
                     "Whitelisted.", now)
        return hit

    def remove_whitelist_domain(self, value: object, removed_by: object = "", source: str = "api",
                                now: datetime | None = None) -> Result:
        raw = self._raw(value)
        refused = self._not_leader("remove", "whitelist_domain", raw)
        if refused:
            return refused
        now = now or db.utcnow()
        with self.locked() as conn:
            try:
                actor = clean_text(removed_by, "removed_by", NAME_MAX)
                domain = normalize_domain(value)
            except Rejected as error:
                return self._reject(conn, "remove", "whitelist_domain", raw, error, "", source, now)
            with db.transaction(conn):
                gone = conn.execute("DELETE FROM whitelist_domains WHERE domain=?", (domain,)).rowcount
                db.audit(conn, "remove", "whitelist_domain", domain, "removed" if gone else "not_found", "",
                         actor, "", source, "", now)
        if not gone:
            return Result("not_found", "This domain is not on the whitelist.")
        return Result("removed", "The domain is no longer on the whitelist.", data={"domain": domain})

    def list_whitelist_domains(self, limit: int = 1000, offset: int = 0) -> Result:
        with self._reader() as conn:
            total = conn.execute("SELECT COUNT(*) FROM whitelist_domains").fetchone()[0]
            rows = conn.execute("SELECT domain, added_at, added_by, reason FROM whitelist_domains"
                                " ORDER BY domain LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return Result("ok", f"{total} entries on the domain whitelist.",
                      data={"total": total, "limit": limit, "offset": offset, "entries": [dict(r) for r in rows]})

    def bulk_whitelist_domains(self, values: Iterable[object], reason: object = "", added_by: object = "",
                               dry_run: bool = False, source: str = "api", now: datetime | None = None) -> Result:
        values = list(values)
        refused = None if dry_run else self._not_leader("bulk_add", "whitelist_domain", f"{len(values)} entries")
        if refused:
            return refused
        now = now or db.utcnow()
        try:
            reason_t, actor = self._texts(reason, added_by)
        except Rejected as error:
            return Result("rejected", error.message, code=error.code)
        rejected: list[dict[str, str]] = []
        domains: set[str] = set()
        for value in values:
            try:
                domains.add(normalize_domain(value))
            except Rejected as error:
                rejected.append({"value": self._raw(value), "reason_code": error.code, "message": error.message})

        with (self._reader() if dry_run else self.locked()) as conn:
            existing = {row[0] for row in conn.execute("SELECT domain FROM whitelist_domains")}
            new = sorted(domains - existing)
            if dry_run:
                removed = sorted(row[0] for row in conn.execute("SELECT domain FROM blocked_domains")
                                 if row[0] in set(new))
                urls = self.urls_covered(conn, domains=new)
            else:
                stamp = db.to_iso(now)
                with db.transaction(conn):
                    conn.executemany("INSERT INTO whitelist_domains(domain, added_at, added_by, reason)"
                                     " VALUES (?,?,?,?)", [(d, stamp, actor, reason_t) for d in new])
                    for d in new:
                        db.audit(conn, "add", "whitelist_domain", d, "added", "", actor, "", source, "bulk", now)
                    for item in rejected:
                        db.audit(conn, "add", "whitelist_domain", item["value"], "rejected", item["reason_code"],
                                 actor, "", source, "bulk", now)
                    removed = self._purge_domains(conn, set(new), actor, source, now)
                    urls = self._purge_urls(conn, actor, source, now, domains=new)
                self._rebuild_after_whitelist(conn, "domains", removed, urls)
        summary = {"dry_run": dry_run, "received": len(values), "added": len(new),
                   "already_exists": len(domains) - len(new), "rejected": rejected,
                   "removed_from_blocklist": removed, "urls_removed_from_blocklist": urls}
        verb = "would be" if dry_run else "were"
        return Result("ok", f"{len(new)} entries {verb} added. {len(rejected)} entries {verb} rejected. "
                            f"{len(removed)} blocked domains and {len(urls)} blocked URLs {verb} removed.",
                      data=summary)
