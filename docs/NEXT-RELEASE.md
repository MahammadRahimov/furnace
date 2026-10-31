# Next release: v0.8.0 (not released)

What changes in the next release, compared with v0.7.0.

How a release happens (user decision 2026-11-01):

1. Every finished change is added to this list. The lab can be updated and tried at any time.
2. Nothing is released until the user says "update" or "new version".
3. Then: the full tests (unit tests, the 11-system integration round, the package tests, and the Docker test
   when Docker changed), then the version on GitHub and the release with the packages.
4. After the release, this list is emptied for the version after it.

## What changes

1. **Upload a blocklist file through the API.** New: `POST /api/v1/ips/bulk` and `POST /api/v1/domains/bulk`.
   A whole file of IPs or domains in one request, with the same rules as `furnace import-ips` (dates, whitelist,
   archive, backup first, all or nothing, the list limit). Options: `dry_run`, `permanent`, `old_ips`,
   `date_format`, `reason`, `added_by`. Needs the blocklist write permission.
2. **Docker image** (step 1): Furnace in one container per host, settings in `docker-compose.yml`, the same
   failover with keepalived. README section 3.6. *(Paused by the user: in this release or later is still open.)*
3. **Replication over HTTPS for Docker.** The servers (`.deb`/`.rpm`) keep SSH replication.
4. **Small changes on the servers:** nginx no longer logs `/health` (asked every 5 seconds), and the keepalived
   notify script also works without systemd.
5. **Docs:** README, `docs/API.md` and the roadmap are updated for the changes above.
