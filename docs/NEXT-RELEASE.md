# Next release: v0.8.0 (not released)

The list of every change since v0.7.0 that goes into the next release, and the tests it needs.
The process (user decision 2026-11-01):

1. Every finished change is added to this list.
2. The lab (test environment) is updated with the new code.
3. The user and Claude test in the lab.
4. Even when everything is OK, nothing is released yet. Only when the user says "update" or "new version":
   check this list, then update the version on GitHub and publish the release with the packages.
   After the release, this list starts again for the version after it.

## Changes

| # | Change | Who sees it | Status | Commit |
|---|---|---|---|---|
| 1 | **Bulk upload of blocked IPs and domains through the API**: `POST /api/v1/ips/bulk`, `POST /api/v1/domains/bulk`. A whole file in one request, the same rules as `furnace import-ips` (dates, whitelist, archive, backup, all or nothing, `MAX_IPS`), `dry_run`, `permanent`, `old_ips`, `date_format`. Blocklist write. Leader only. | servers and Docker | coded, unit tests pass, in the lab | 7fff051 |
| 2 | **Docker image (step 1)**: one container per host (API, nginx, keepalived, jobs), settings in `docker-compose.yml`, follower bundle, `docker exec` for tokens. README section 3.6. | Docker users | coded, Docker test 48/48, **paused by the user** (decide: in this release or later) | f78c4ec, 1ff9226 |
| 3 | **Replication over HTTPS** (`POST /replica/snapshot` with a shared secret). Only on when `FURNACE_REPLICA_SECRET_FILE` is set, so only in Docker. Servers keep SSH. | Docker users | coded, unit tests pass | d075111 |
| 4 | Small shared changes: nginx does not log `/health` (keepalived asks every 5 seconds); an empty replication block in the nginx template; `furnace-notify` works without systemd. | servers and Docker | coded, in the lab | f78c4ec |
| 5 | Version 0.8.0 in the code; docs (README, `docs/API.md`, roadmap). | everyone | done | 1ff9226 |

## Tests before the release

| Test | Status |
|---|---|
| Unit tests (`pytest`) | 205 pass (2026-11-01) |
| Lab: upgrade from 0.7.0 to 0.8.0 with the data kept | done 2026-11-01 (both servers, self-test passed) |
| Lab: bulk upload of IPs and domains (dry run, real, skipped lines) | started (dry run through the test UI) |
| Lab: failover and replication on the new version | to do |
| Integration round on the 11 systems (`tests/integration/run.sh`) | to do |
| Docker test (`tests/docker/run.sh`), only if item 2 is in this release | to do |

## Open questions for the user

- Item 2 (Docker): ship it in v0.8.0, or keep it out of the release notes until the Docker work goes on?
