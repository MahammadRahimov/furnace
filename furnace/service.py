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
from datetime import datetime
from typing import Any, Iterable, Iterator

from . import db, edl
from .config import Settings
from .locking import FileLock
from .rangeset import RangeSet
from .validation import (Rejected, clean_text, ip_sort_key, normalize_domain, range_hex,
                         to_hex, validate_ip, validate_whitelist_cidr)

log = logging.getLogger("furnace")

REASON_MAX = 500
NAME_MAX = 100
INCIDENT_MAX = 100


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
        writers = {"ips": edl.write_blocklist, "domains": edl.write_domains,
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
                error: Rejected, actor: str, incident: str, source: str,
                now: datetime | None) -> Result:
        with db.transaction(conn):
            db.audit(conn, action, object_type, value, "rejected", error.code, actor, incident,
                     source, error.message, now)
        return Result("rejected", error.message, code=error.code)

    @staticmethod
    def _texts(reason: object, added_by: object, incident_id: object) -> tuple[str, str, str]:
        return (clean_text(reason, "reason", REASON_MAX),
                clean_text(added_by, "added_by", NAME_MAX),
                clean_text(incident_id, "incident_id", INCIDENT_MAX))

    @staticmethod
    def _raw(value: object) -> str:
        return value.strip()[:300] if isinstance(value, str) else repr(value)[:300]

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
               incident_id: object = "", source: str = "api", now: datetime | None = None) -> Result:
        raw = self._raw(value)
        refused = self._not_leader("add", "ip", raw)
        if refused:
            return refused
        now = now or db.utcnow()
        with self.locked() as conn:
            try:
                reason_t, actor, incident = self._texts(reason, added_by, incident_id)
            except Rejected as error:
                return self._reject(conn, "add", "ip", raw, error, "", "", source, now)
            try:
                ip = validate_ip(value, self.settings.test_ranges_allowed())
                cidr = self._ip_whitelisted(conn, ip)
                if cidr:
                    raise Rejected("whitelisted", f"This IP is covered by the whitelist entry {cidr}. "
                                                  "It cannot be blocked.")
            except Rejected as error:
                return self._reject(conn, "add", "ip", raw, error, actor, incident, source, now)

            text = str(ip)
            stamp = db.to_iso(now)
            with db.transaction(conn):
                existing = conn.execute("SELECT * FROM blocked_ips WHERE ip=?", (text,)).fetchone()
                if existing:
                    db.audit(conn, "add", "ip", text, "already_exists", "", actor, incident, source,
                             f"Already blocked since {existing['added_at']}.", now)
                    return Result("already_exists",
                                  f"This IP is already blocked since {existing['added_at']}. Nothing changed.",
                                  data={"entry": self._ip_entry(existing)})
                if self.settings.max_ips:
                    count = conn.execute("SELECT COUNT(*) FROM blocked_ips").fetchone()[0]
                    if count >= self.settings.max_ips:
                        db.audit(conn, "add", "ip", text, "rejected", "limit_reached", actor, incident,
                                 source, f"Limit is {self.settings.max_ips}.", now)
                        return Result("rejected", Rejected("limit_reached").message, code="limit_reached")
                archived = conn.execute("SELECT 1 FROM archived_ips WHERE ip=?", (text,)).fetchone()
                if archived:
                    conn.execute("DELETE FROM archived_ips WHERE ip=?", (text,))
                conn.execute(
                    "INSERT INTO blocked_ips(ip, version, added_at, aging_from, added_by, reason, incident_id)"
                    " VALUES (?,?,?,?,?,?,?)", (text, ip.version, stamp, stamp, actor, reason_t, incident))
                db.audit(conn, "add", "ip", text, "restored" if archived else "added", "", actor,
                         incident, source, "Moved back from the archive." if archived else "", now)
            self._rebuild(conn, "ips", *(["archive"] if archived else []))
            entry = {"ip": text, "added_at": stamp, "added_by": actor, "reason": reason_t,
                     "incident_id": incident}
            if archived:
                return Result("added", "This IP was in the archive. It is blocked again with a new date.",
                              data={"entry": entry, "restored_from_archive": True})
            return Result("added", "The IP is now blocked.", data={"entry": entry})

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
                return self._reject(conn, "remove", "ip", raw, error, "", "", source, now)
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
            if row:
                return Result("found", "This IP is blocked.",
                              data={"list": "active", "entry": self._ip_entry(row), "whitelisted_by": cidr})
            row = conn.execute("SELECT * FROM archived_ips WHERE ip=?", (text,)).fetchone()
            if row:
                entry = {k: row[k] for k in ("ip", "added_at", "archived_at", "added_by", "reason", "incident_id")}
                return Result("found", "This IP is in the archive. It is not blocked now.",
                              data={"list": "archive", "entry": entry, "whitelisted_by": cidr})
        return Result("not_found", "This IP is not in the blocklist or the archive.",
                      data={"whitelisted_by": cidr})

    @staticmethod
    def _ip_entry(row: sqlite3.Row) -> dict[str, str]:
        return {k: row[k] for k in ("ip", "added_at", "added_by", "reason", "incident_id")}

    # ------------------------------------------------------------------ domains

    def add_domain(self, value: object, reason: object = "", added_by: object = "",
                   incident_id: object = "", source: str = "api", now: datetime | None = None) -> Result:
        raw = self._raw(value)
        refused = self._not_leader("add", "domain", raw)
        if refused:
            return refused
        now = now or db.utcnow()
        with self.locked() as conn:
            try:
                reason_t, actor, incident = self._texts(reason, added_by, incident_id)
            except Rejected as error:
                return self._reject(conn, "add", "domain", raw, error, "", "", source, now)
            try:
                domain = normalize_domain(value)
                if self._domain_whitelisted(conn, domain):
                    raise Rejected("whitelisted")
            except Rejected as error:
                return self._reject(conn, "add", "domain", raw, error, actor, incident, source, now)
            stamp = db.to_iso(now)
            with db.transaction(conn):
                existing = conn.execute("SELECT * FROM blocked_domains WHERE domain=?", (domain,)).fetchone()
                if existing:
                    db.audit(conn, "add", "domain", domain, "already_exists", "", actor, incident, source,
                             f"Already blocked since {existing['added_at']}.", now)
                    return Result("already_exists",
                                  f"This domain is already blocked since {existing['added_at']}. Nothing changed.",
                                  data={"entry": dict(existing)})
                if self.settings.max_domains:
                    count = conn.execute("SELECT COUNT(*) FROM blocked_domains").fetchone()[0]
                    if count >= self.settings.max_domains:
                        db.audit(conn, "add", "domain", domain, "rejected", "limit_reached", actor, incident,
                                 source, f"Limit is {self.settings.max_domains}.", now)
                        return Result("rejected", Rejected("limit_reached").message, code="limit_reached")
                conn.execute("INSERT INTO blocked_domains(domain, added_at, added_by, reason, incident_id)"
                             " VALUES (?,?,?,?,?)", (domain, stamp, actor, reason_t, incident))
                db.audit(conn, "add", "domain", domain, "added", "", actor, incident, source, "", now)
            self._rebuild(conn, "domains")
            return Result("added", "The domain is now blocked.",
                          data={"entry": {"domain": domain, "added_at": stamp, "added_by": actor,
                                          "reason": reason_t, "incident_id": incident}})

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
                return self._reject(conn, "remove", "domain", raw, error, "", "", source, now)
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
        if row:
            return Result("found", "This domain is blocked.", data={"entry": dict(row), "whitelisted": whitelisted})
        return Result("not_found", "This domain is not in the blocklist.", data={"whitelisted": whitelisted})

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
                reason_t, actor, _ = self._texts(reason, added_by, "")
                net = validate_whitelist_cidr(value)
            except Rejected as error:
                return self._reject(conn, "add", "whitelist_ip", raw, error, "", "", source, now)
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
            if removed:
                self._rebuild(conn, "ips")
            return Result("added", self._wl_message(removed, "IP"),
                          data={"cidr": cidr, "removed_from_blocklist": removed})

    @staticmethod
    def _wl_message(removed: list[str], kind: str) -> str:
        if not removed:
            return "The entry is on the whitelist now."
        return (f"The entry is on the whitelist now. {len(removed)} blocked {kind}"
                f"{'s were' if len(removed) != 1 else ' was'} removed from the blocklist.")

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
                return self._reject(conn, "remove", "whitelist_ip", raw, error, "", "", source, now)
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
            reason_t, actor, _ = self._texts(reason, added_by, "")
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
                if removed:
                    self._rebuild(conn, "ips")
        summary = {"dry_run": dry_run, "received": len(values), "added": len(new),
                   "already_exists": len(nets) - len(new), "rejected": rejected,
                   "removed_from_blocklist": removed}
        verb = "would be" if dry_run else "were"
        return Result("ok", f"{len(new)} entries {verb} added. {len(rejected)} entries {verb} rejected. "
                            f"{len(removed)} blocked IPs {verb} removed.", data=summary)

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
                reason_t, actor, _ = self._texts(reason, added_by, "")
                domain = normalize_domain(value)
            except Rejected as error:
                return self._reject(conn, "add", "whitelist_domain", raw, error, "", "", source, now)
            with db.transaction(conn):
                if self._domain_whitelisted(conn, domain):
                    db.audit(conn, "add", "whitelist_domain", domain, "already_exists", "", actor, "", source, "", now)
                    return Result("already_exists", "This domain is already on the whitelist. Nothing changed.",
                                  data={"domain": domain})
                conn.execute("INSERT INTO whitelist_domains(domain, added_at, added_by, reason) VALUES (?,?,?,?)",
                             (domain, db.to_iso(now), actor, reason_t))
                db.audit(conn, "add", "whitelist_domain", domain, "added", "", actor, "", source, "", now)
                removed = self._purge_domains(conn, {domain}, actor, source, now)
            if removed:
                self._rebuild(conn, "domains")
            return Result("added", self._wl_message(removed, "domain"),
                          data={"domain": domain, "removed_from_blocklist": removed})

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
                return self._reject(conn, "remove", "whitelist_domain", raw, error, "", "", source, now)
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
            reason_t, actor, _ = self._texts(reason, added_by, "")
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
                if removed:
                    self._rebuild(conn, "domains")
        summary = {"dry_run": dry_run, "received": len(values), "added": len(new),
                   "already_exists": len(domains) - len(new), "rejected": rejected,
                   "removed_from_blocklist": removed}
        verb = "would be" if dry_run else "were"
        return Result("ok", f"{len(new)} entries {verb} added. {len(rejected)} entries {verb} rejected. "
                            f"{len(removed)} blocked domains {verb} removed.", data=summary)
