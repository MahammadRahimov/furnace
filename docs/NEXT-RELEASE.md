# Next release: v0.8.0 (not released)

What changes in the next release, compared with v0.7.0.

How a release happens (user decision 2026-11-01):

1. Every finished change is added to this list. The lab can be updated and tried at any time.
2. Nothing is released until the user says "update" or "new version".
3. Then: the full tests (unit tests, the 11-system integration round and the package tests), then the version
   on GitHub and the release with the packages.
4. After the release, this list is emptied for the version after it.

## What changes

1. **Upload a blocklist file through the API.** New: `POST /api/v1/ips/bulk` and `POST /api/v1/domains/bulk`.
   A whole file of IPs or domains in one request, with the same rules as `furnace import-ips` (dates, whitelist,
   archive, backup first, all or nothing, the list limit). Options: `dry_run`, `permanent`, `old_ips`,
   `date_format`, `reason`, `added_by`. Needs the blocklist write permission.
2. **Replication over HTTPS.** Every minute the leader sends the snapshot to `https://PEER_IP/replica/snapshot`
   with a replication secret; the follower loads it and answers at once, so `furnace status` on the leader shows
   whether it worked. The secret comes from the certificate key both servers already share, so an upgrade needs no
   new bundle. For this version the leader falls back to SSH when HTTPS does not work (an older follower during an
   upgrade); the version after 0.8 removes SSH replication.
3. **`incident_id` is removed.** It can mean different things to different callers. A request that still sends
   it gets `400` with a clear message (callers must remove it before the upgrade). The upgrade deletes every stored
   incident number; the API answers, the exports and the archive CSV no longer have it. An incident number can go
   into `reason`.
4. **Small changes on the servers:** nginx no longer logs `/health` (asked every 5 seconds), the keepalived notify
   script also works without systemd, and `furnace status` shows the last replication result.
5. **Two failover fixes** (found by breaking each part of the leader in the lab):
   - **keepalived crash:** after a `kill -9`, the crashed server kept the shared IP and still called itself the
     leader, so both servers owned the IP and took changes. Setup now adds a systemd drop-in
     (`/etc/systemd/system/keepalived.service.d/furnace.conf`, `Restart=on-failure`): keepalived starts again,
     drops the IP and joins as the standby. Uninstall removes the drop-in.
   - **Expired certificate:** the health check verified the certificate, which both servers share. When it
     expired, both servers failed the check at once and nobody held the shared IP. The health check now ignores
     the certificate (it only talks to 127.0.0.1); the daily certificate warnings report the expiry.
   - The integration round now tests both cases.
6. **Docs:** README (replication, upgrade from 0.7 to 0.8, network checklist, files), `docs/API.md` and the roadmap.

Not in this release: the Docker image. It is kept on the branch `docker` (user decision 2026-11-01).
