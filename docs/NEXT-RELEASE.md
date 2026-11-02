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
2. **Replication over HTTPS.** Every 20 seconds the leader sends the snapshot to `https://PEER_IP/replica/snapshot`
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
6. **Replication every 20 seconds** (was every minute). If the leader dies, at most about 20 seconds of changes
   can be lost instead of a minute, and a deleted or broken list file on the leader is rebuilt sooner. One copy is
   small (lab: 247 KB, about 0.25 s). Real-time replication stays a later idea (roadmap, high-availability phase).
7. **IPs stay blocked 90 days by default** (was 30). The setup question "Days an IP stays blocked" now suggests
   90. Servers that are already set up keep their saved answer on upgrade; to change it, run `sudo furnace setup`
   on both servers with the new number. A longer time also applies to the IPs already on the list (their expiry
   is counted from their block date); IPs that were already moved to the archive stay there.
8. **Fix: setup no longer leaves its test IP on the list.** When setup ran on the follower while that follower held
   the shared IP (after a failover, for example during an upgrade), its "follower refuses changes" test added
   `192.0.2.123` to the real list and never removed it. Setup now checks the role first and skips that test.
9. **A URL blocklist.** A third list next to IPs and domains, for a firewall's URL list (on Palo Alto: an EDL of
   type URL List, used as a custom URL category; it blocks the web traffic itself, not only the DNS lookup).
   - API: `POST /api/v1/urls`, `GET` and `DELETE /api/v1/urls?url=...`, and `POST /api/v1/urls/bulk` for a file
     (`furnace import-urls` on the server). The blocklist permission, as for IPs and domains.
   - The list for the firewall: `https://SHARED_IP/edl/urls.txt` (same login). The upgrade adds it, empty.
   - Furnace cleans every URL into the firewall's form: `http://` and `https://` removed, lower case, no `#...`,
     a host alone ends with `/`. The answer shows the stored form.
   - URLs never expire. Wildcards are allowed as whole parts (`*.bad.example.com/`, `^.bad.example.com/`); too
     wide ones are refused (`*.com/`, `example.*/`, `*.co.uk/`).
   - The whitelist protects by host: a domain on the domain whitelist protects itself and all its subdomains,
     also against wildcards that cover it. A URL with an IP host is checked against the IP whitelist. Adding a
     whitelist entry removes the URLs it protects.
   - The limit `MAX_URLS` (setup uses the same number as the other lists). Archive record, export, stats,
     replication and the health check include URLs. Schema 6 (new tables only).
10. **The domain whitelist protects subdomains, and shared platforms cannot be whitelisted.**
    - A domain on the whitelist now protects itself **and all its subdomains** in the domain list (before: exact
      only), as it already does in the URL list. Adding it removes the blocked subdomains; blocking one later is
      refused. The upgrade does this once for the whitelist entries that already exist (audit source `upgrade`).
    - Shared platforms and public names cannot be whitelisted (`422` `whitelist_not_allowed`): `github.io`,
      `blogspot.com`, `co.uk`, and names that cover one, such as `amazonaws.com`. Furnace uses the Public Suffix
      List (publicsuffix.org, MPL 2.0), bundled in the package and updated with each release.
    - Your own names: `/etc/furnace/whitelist-refuse.txt` (setup makes it empty once). A name there also refuses
      every name that covers it.
    - `furnace status` warns about old whitelist entries that would be refused today. They are not removed.
    - IPs: no change. A whitelisted range already protects every IP in it.
11. **Docs:** README (replication, upgrade from 0.7 to 0.8, network checklist, files), `docs/API.md` and the roadmap.

Not in this release: the Docker image. It is kept on the branch `docker` (user decision 2026-11-01).
