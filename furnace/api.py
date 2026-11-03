"""The Furnace HTTP API (Flask, JSON).

Every /api/ call needs a token (Authorization: Bearer ...). Blocklist calls (IPs, domains
and URLs) need the blocklist permission, whitelist calls the whitelist permission.
An IP that sends too many wrong tokens is locked for a while (429).
Internal errors are logged on the server and never shown to the caller.
"""

from __future__ import annotations

import logging
import os
import tempfile
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from flask import Flask, Response, g, jsonify, request
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from . import __version__, auth, db, importer, replica
from .config import Settings
from .locking import LockTimeout
from .service import Furnace, Result

log = logging.getLogger("furnace")

STATUS_CODES = {"added": 201, "already_exists": 200, "removed": 200, "found": 200, "ok": 200,
                "not_found": 404, "rejected": 422}
MAX_BULK_ENTRIES = 200_000


def _reply(result: Result) -> tuple[Response, int]:
    code = STATUS_CODES.get(result.status, 200)
    if result.code == "not_leader":
        code = 503
    return jsonify(result.to_dict()), code


def _error(status: str, message: str, http: int) -> tuple[Response, int]:
    return jsonify({"status": status, "message": message}), http


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or Settings.load()
    furnace = Furnace(settings)
    furnace.startup()

    app = Flask("furnace")
    app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024
    app.config["JSON_SORT_KEYS"] = False  # Flask 2.0 and 2.1 (Ubuntu 22.04)
    if hasattr(app, "json"):  # Flask 2.2 and newer
        app.json.sort_keys = False  # type: ignore[attr-defined]
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)  # type: ignore[method-assign]
    app.extensions["furnace"] = furnace

    def require(list_name: str, action: str) -> Callable:
        """list_name is blocklist or whitelist, action is read or write."""
        def decorator(view: Callable) -> Callable:
            @wraps(view)
            def wrapper(*args: Any, **kwargs: Any):
                caller = request.remote_addr or "unknown"
                wait = auth.locked_for(settings, caller)
                if wait:
                    resp, code = _error("locked", "Too many wrong tokens from this address. "
                                        f"Try again in {wait} seconds.", 429)
                    resp.headers["Retry-After"] = str(wait)
                    return resp, code
                header = request.headers.get("Authorization", "")
                given = header[7:].strip() if header[:7].lower() == "bearer " else ""
                client = auth.find_client(settings, given)
                if client is not None and client.expired():
                    # A real token, so it does not count towards the lockout.
                    log.warning("Refused %s %s from %s: token '%s' expired on %s.", request.method,
                                request.path, caller, client.name, client.expires_at)
                    resp, code = _error("unauthorized", f"This token expired on {client.expires_at[:10]}. "
                                        "Ask the Furnace admin for a new one.", 401)
                    resp.headers["WWW-Authenticate"] = 'Bearer error="invalid_token"'
                    return resp, code
                if client is None or not client.may(list_name, action):
                    if given and client is None and auth.record_failure(settings, caller):
                        log.warning("Locked %s for %s minutes after %s wrong tokens.",
                                    caller, settings.lockout_minutes, settings.lockout_failures)
                    log.warning("Refused %s %s from %s: %s.", request.method, request.path, caller,
                                "missing token" if not given else "wrong token" if client is None
                                else f"token '{client.name}' has no {list_name} {action} permission")
                    message = ("The token is missing or wrong." if client is None else
                               f"This token may not do this. It needs the {list_name} {action} permission.")
                    resp, code = _error("unauthorized", message, 401)
                    resp.headers["WWW-Authenticate"] = "Bearer"
                    return resp, code
                auth.mark_used(settings, client)
                g.source = f"api:{client.name}@{caller}"
                return view(*args, **kwargs)
            return wrapper
        return decorator

    def body() -> dict[str, Any]:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise _BadRequest("The body must be a JSON object.")
        return data

    def no_incident(data: dict[str, Any]) -> None:
        """incident_id was removed in 0.8: a caller that still sends it is told so (user decision)."""
        if "incident_id" in data:
            raise _BadRequest("The field incident_id is no longer supported (removed in Furnace 0.8). "
                              "Remove it from the request. Put the incident number into reason if you need it.")

    def actor() -> Any:
        data = request.get_json(silent=True) if request.data else None
        if isinstance(data, dict) and data.get("removed_by"):
            return data["removed_by"]
        return request.args.get("removed_by", "")

    # -------------------------------------------------------------- health

    @app.get("/health")
    def health():
        try:
            conn = db.connect(settings.db_path)
            try:
                ips = conn.execute("SELECT COUNT(*) FROM blocked_ips").fetchone()[0]
                domains = conn.execute("SELECT COUNT(*) FROM blocked_domains").fetchone()[0]
                urls = conn.execute("SELECT COUNT(*) FROM blocked_urls").fetchone()[0]
            finally:
                conn.close()
            if not all(p.is_file() for p in (settings.blocklist_path, settings.domains_path, settings.urls_path)):
                raise RuntimeError("EDL files are missing")
        except Exception:
            log.exception("Health check failed.")
            return _error("error", "The service is not healthy.", 503)
        return jsonify({"status": "ok", "version": __version__,
                        "role": "leader" if settings.is_leader() else "follower",
                        "ips": ips, "domains": domains, "urls": urls})

    # -------------------------------------------------------------- replication

    if settings.replica_secret_file is not None:
        @app.post("/replica/snapshot")
        def receive_snapshot():
            """The leader sends a gzip snapshot every 20 seconds. nginx lets only the other server in."""
            caller = request.remote_addr or "unknown"
            header = request.headers.get("Authorization", "")
            given = header[7:].strip() if header[:7].lower() == "bearer " else ""
            if not replica.secret_matches(settings.replica_secret_file, given):  # type: ignore[arg-type]
                log.warning("Refused a snapshot from %s: wrong replication secret.", caller)
                return _error("unauthorized", "The replication secret is missing or wrong.", 401)
            request.max_content_length = replica.MAX_SNAPSHOT_BYTES
            try:
                counts = replica.receive_snapshot(settings, request.stream)
            except replica.ReplicaError as exc:
                log.warning("Refused a snapshot from %s: %s", caller, exc)
                return _error("rejected", str(exc), 409 if settings.is_leader() else 422)
            return jsonify({"status": "ok", "applied": counts})

    # -------------------------------------------------------------- IPs

    @app.post("/api/v1/ips")
    @require("blocklist", "write")
    def add_ip():
        d = body()
        no_incident(d)
        return _reply(furnace.add_ip(d.get("ip"), d.get("reason"), d.get("added_by"),
                                     source=g.source, permanent=d.get("permanent")))

    @app.get("/api/v1/ips/<ip>")
    @require("blocklist", "read")
    def get_ip(ip: str):
        return _reply(furnace.get_ip(ip))

    @app.delete("/api/v1/ips/<ip>")
    @require("blocklist", "write")
    def remove_ip(ip: str):
        return _reply(furnace.remove_ip(ip, actor(), source=g.source))

    # -------------------------------------------------------------- domains

    @app.post("/api/v1/domains")
    @require("blocklist", "write")
    def add_domain():
        d = body()
        no_incident(d)
        return _reply(furnace.add_domain(d.get("domain"), d.get("reason"), d.get("added_by"), source=g.source))

    @app.get("/api/v1/domains/<domain>")
    @require("blocklist", "read")
    def get_domain(domain: str):
        return _reply(furnace.get_domain(domain))

    @app.delete("/api/v1/domains/<domain>")
    @require("blocklist", "write")
    def remove_domain(domain: str):
        return _reply(furnace.remove_domain(domain, actor(), source=g.source))

    # -------------------------------------------------------------- URLs
    # A URL has slashes, so it goes in the query string (?url=...) or the JSON body, not in the path.

    def url_param() -> Any:
        if "url" in request.args:
            return request.args.get("url")
        data = request.get_json(silent=True) if request.data else None
        if isinstance(data, dict) and "url" in data:
            return data["url"]
        raise _BadRequest("Give the URL as ?url=... (URL-encoded) or in the JSON body as \"url\".")

    @app.post("/api/v1/urls")
    @require("blocklist", "write")
    def add_url():
        d = body()
        no_incident(d)
        return _reply(furnace.add_url(d.get("url"), d.get("reason"), d.get("added_by"), source=g.source))

    @app.get("/api/v1/urls")
    @require("blocklist", "read")
    def get_url():
        return _reply(furnace.get_url(url_param()))

    @app.delete("/api/v1/urls")
    @require("blocklist", "write")
    def remove_url():
        return _reply(furnace.remove_url(url_param(), actor(), source=g.source))

    # -------------------------------------------------------------- whitelist

    def paging() -> tuple[int, int]:
        try:
            limit = min(max(int(request.args.get("limit", 1000)), 1), 10000)
            offset = max(int(request.args.get("offset", 0)), 0)
        except ValueError:
            raise _BadRequest("limit and offset must be whole numbers.") from None
        return limit, offset

    def bulk_input() -> tuple[list[Any], bool, Any, Any]:
        """JSON {"entries": [...], "dry_run": true} or plain text, one entry per line."""
        if request.mimetype == "text/plain":
            text = request.get_data(as_text=True)
            entries: list[Any] = [line.strip() for line in text.splitlines()
                                  if line.strip() and not line.lstrip().startswith("#")]
            args = request.args
            dry = args.get("dry_run", "false").lower() in ("1", "true", "yes")
            return entries, dry, args.get("reason", ""), args.get("added_by", "")
        d = body()
        entries = d.get("entries")
        if not isinstance(entries, list):
            raise _BadRequest("The field entries must be a list.")
        dry = d.get("dry_run", False)
        if not isinstance(dry, bool):
            raise _BadRequest("The field dry_run must be true or false.")
        return entries, dry, d.get("reason"), d.get("added_by")

    def check_bulk_size(entries: list[Any]) -> None:
        if len(entries) > MAX_BULK_ENTRIES:
            raise _BadRequest(f"Send at most {MAX_BULK_ENTRIES} entries per request.")

    def flag(name: str, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("1", "true", "yes", "0", "false", "no", ""):
            return value.lower() in ("1", "true", "yes")
        raise _BadRequest(f"The field {name} must be true or false.")

    def bulk_file(kind: str) -> tuple[Response, int]:
        """A whole file of blocked IPs or domains, with the same rules as 'furnace import-ips'.

        Plain text body (the file as it is; options in the query string) or JSON
        {"entries": ["ip", "ip,date,reason", ...], "dry_run": true, ...}.
        """
        if request.mimetype == "application/json":
            d = body()
            entries = d.get("entries")
            if not isinstance(entries, list) or not all(isinstance(e, str) for e in entries):
                raise _BadRequest("The field entries must be a list of lines (strings).")
            text = "\n".join(entries)
            options: dict[str, Any] = d
            no_incident(d)
        else:
            text = request.get_data(as_text=True)
            options = dict(request.args)
            no_incident(options)
        lines = sum(1 for line in text.splitlines() if line.strip())
        if lines > MAX_BULK_ENTRIES:
            raise _BadRequest(f"Send at most {MAX_BULK_ENTRIES} lines per request.")
        if not lines:
            raise _BadRequest("The upload has no lines.")
        dry_run = flag("dry_run", options.get("dry_run", False))
        added_by = options.get("added_by") or g.source.split("@")[0]
        reason = options.get("reason") or "uploaded through the API"
        if not isinstance(added_by, str) or not isinstance(reason, str):
            raise _BadRequest("added_by and reason must be text.")
        folder = settings.data_dir / "uploads"
        folder.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=folder, prefix=f"api-{kind}-", suffix=".txt")
        source = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            if kind == "ips":
                old_ips = options.get("old_ips", "archive")
                date_format = options.get("date_format") or None
                if old_ips not in ("archive", "fresh"):
                    raise _BadRequest("The field old_ips must be 'archive' or 'fresh'.")
                if date_format is not None and not isinstance(date_format, str):
                    raise _BadRequest("The field date_format must be text, for example %d/%m/%Y.")
                report = importer.import_ips(settings, source, dry_run=dry_run, date_format=date_format,
                                             old_ips=old_ips, added_by=added_by, reason=reason,
                                             permanent=flag("permanent", options.get("permanent", False)))
            elif kind == "urls":
                report = importer.import_urls(settings, source, dry_run=dry_run, added_by=added_by, reason=reason)
            else:
                report = importer.import_domains(settings, source, dry_run=dry_run, added_by=added_by,
                                                 reason=reason)
        except importer.ImportError_ as exc:
            if not settings.is_leader():
                return _error("not_leader", "This server is the standby. Send the upload to the shared IP.", 503)
            return _error("rejected", str(exc), 422)
        finally:
            source.unlink(missing_ok=True)
        skipped = [{"line": s.line_no, "reason_code": s.reason, "text": s.text[:200]} for s in report.skipped]
        answer: dict[str, Any] = {
            "status": "ok",
            "message": report.summary().split("\nBackup:")[0].split("\nReport:")[0].replace("\n", " "),
            "dry_run": dry_run, "received": report.total_lines, "added": report.added_active,
        }
        if kind == "ips":
            answer.update(permanent=report.permanent, archived=report.added_archive)
        answer.update(blocked_again=report.restored, skipped=len(skipped), skipped_lines=skipped[:1000],
                      report=Path(report.report_file).name)
        log.info("Bulk %s upload by %s: %s", kind, g.source, answer["message"])
        return jsonify(answer), 200

    @app.post("/api/v1/ips/bulk")
    @require("blocklist", "write")
    def bulk_ips():
        return bulk_file("ips")

    @app.post("/api/v1/domains/bulk")
    @require("blocklist", "write")
    def bulk_domains():
        return bulk_file("domains")

    @app.post("/api/v1/urls/bulk")
    @require("blocklist", "write")
    def bulk_urls():
        return bulk_file("urls")

    @app.get("/api/v1/whitelist/check/<value>")
    @require("whitelist", "read")
    def check_whitelist(value: str):
        return _reply(furnace.check_whitelist(value))

    @app.post("/api/v1/whitelist/ips")
    @require("whitelist", "write")
    def add_whitelist_ip():
        d = body()
        value = d.get("entry") or d.get("cidr") or d.get("ip")
        return _reply(furnace.add_whitelist_ip(value, d.get("reason"), d.get("added_by"), source=g.source))

    @app.get("/api/v1/whitelist/ips")
    @require("whitelist", "read")
    def list_whitelist_ips():
        return _reply(furnace.list_whitelist_ips(*paging()))

    @app.post("/api/v1/whitelist/ips/bulk")
    @require("whitelist", "write")
    def bulk_whitelist_ips():
        entries, dry, reason, who = bulk_input()
        check_bulk_size(entries)
        return _reply(furnace.bulk_whitelist_ips(entries, reason, who, dry_run=dry, source=g.source))

    @app.delete("/api/v1/whitelist/ips/<path:entry>")
    @require("whitelist", "write")
    def remove_whitelist_ip(entry: str):
        return _reply(furnace.remove_whitelist_ip(entry, actor(), source=g.source))

    @app.post("/api/v1/whitelist/domains")
    @require("whitelist", "write")
    def add_whitelist_domain():
        d = body()
        return _reply(furnace.add_whitelist_domain(d.get("domain"), d.get("reason"), d.get("added_by"),
                                                   source=g.source))

    @app.get("/api/v1/whitelist/domains")
    @require("whitelist", "read")
    def list_whitelist_domains():
        return _reply(furnace.list_whitelist_domains(*paging()))

    @app.post("/api/v1/whitelist/domains/bulk")
    @require("whitelist", "write")
    def bulk_whitelist_domains():
        entries, dry, reason, who = bulk_input()
        check_bulk_size(entries)
        return _reply(furnace.bulk_whitelist_domains(entries, reason, who, dry_run=dry, source=g.source))

    @app.delete("/api/v1/whitelist/domains/<domain>")
    @require("whitelist", "write")
    def remove_whitelist_domain(domain: str):
        return _reply(furnace.remove_whitelist_domain(domain, actor(), source=g.source))

    # -------------------------------------------------------------- errors

    @app.errorhandler(_BadRequest)
    def bad_request(error: "_BadRequest"):
        return _error("bad_request", str(error), 400)

    @app.errorhandler(LockTimeout)
    def busy(error: LockTimeout):
        log.error("Lock timeout: %s", error)
        return _error("busy", "The server is busy. Try again in a few seconds.", 503)

    @app.errorhandler(HTTPException)
    def http_error(error: HTTPException):
        messages = {404: "This address does not exist.", 405: "This method is not allowed here.",
                    413: "The request is too large.", 400: "The request is not valid."}
        status = {404: "not_found", 405: "method_not_allowed", 413: "too_large"}.get(error.code or 0, "bad_request")
        return _error(status, messages.get(error.code or 0, "The request is not valid."), error.code or 400)

    @app.errorhandler(Exception)
    def internal(error: Exception):
        log.exception("Unexpected error on %s %s", request.method, request.path)
        return _error("error", "Something went wrong on the server. The error was logged.", 500)

    return app


class _BadRequest(Exception):
    pass
