"""The Furnace HTTP API (Flask, JSON).

Every /api/ call needs a token (Authorization: Bearer ...). Blocklist calls need a
token with the scope block or all, whitelist calls the scope whitelist or all.
An IP that sends too many wrong tokens is locked for a while (429).
Internal errors are logged on the server and never shown to the caller.
"""

from __future__ import annotations

import logging
from functools import wraps
from typing import Any, Callable

from flask import Flask, Response, g, jsonify, request
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from . import __version__, auth, db
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
            finally:
                conn.close()
            if not (settings.blocklist_path.is_file() and settings.domains_path.is_file()):
                raise RuntimeError("EDL files are missing")
        except Exception:
            log.exception("Health check failed.")
            return _error("error", "The service is not healthy.", 503)
        return jsonify({"status": "ok", "version": __version__,
                        "role": "leader" if settings.is_leader() else "follower",
                        "ips": ips, "domains": domains})

    # -------------------------------------------------------------- IPs

    @app.post("/api/v1/ips")
    @require("blocklist", "write")
    def add_ip():
        d = body()
        return _reply(furnace.add_ip(d.get("ip"), d.get("reason"), d.get("added_by"),
                                     d.get("incident_id"), source=g.source))

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
        return _reply(furnace.add_domain(d.get("domain"), d.get("reason"), d.get("added_by"),
                                         d.get("incident_id"), source=g.source))

    @app.get("/api/v1/domains/<domain>")
    @require("blocklist", "read")
    def get_domain(domain: str):
        return _reply(furnace.get_domain(domain))

    @app.delete("/api/v1/domains/<domain>")
    @require("blocklist", "write")
    def remove_domain(domain: str):
        return _reply(furnace.remove_domain(domain, actor(), source=g.source))

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
