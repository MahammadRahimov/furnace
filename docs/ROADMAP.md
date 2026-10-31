# Furnace roadmap

State on 2026-11-01: **v0.7.0 is released** (`.deb` and `.rpm`). v0.8.0 (bulk blocklist upload, HTTPS replication) is coded, not released; the list is in `docs/NEXT-RELEASE.md`. Docker step 1 is coded and parked on the branch `docker` (not in v0.8.0). This file holds the plan for the next steps,
so the work can continue later exactly where it stopped.

## Decisions so far

| Date | Decision |
|---|---|
| 2026-10-26 | Furnace is its own project. Private repo `MahammadRahimov/furnace`. EDL limit 50,000. Self-signed HTTPS certificate. |
| 2026-10-26 | A real tool: one package, then `sudo furnace setup`, which asks questions. |
| 2026-10-27 | No IP rules needed: the API is protected by tokens (hashed, with scopes, lockout), the lists by a login. IP limits are optional (`any` is the default). A UI will come later and needs its own user logins. |
| 2026-10-27 | Vendor-neutral: no XSOAR or Palo Alto in the product. They are only examples in the README (section 15). |
| 2026-10-27 | Run on "every Linux that companies use". **Option A:** Flask and gunicorn are bundled in the package (`packaging/vendor.lock`), not taken from the system. EPEL is allowed but not needed. |
| 2026-10-28 | **The released systems are enough** (Ubuntu, Debian, Red Hat family, Fedora, Amazon Linux). No more systems: the SUSE work is parked on the branch `suse` (coded, Leap 15.6 and 16.0 tests passed, not released). The generic tarball is dropped. |
| 2026-10-28 | **Token permissions instead of fixed scopes.** One token per program, with four check boxes in a grid: rows Blocklist and Whitelist, columns Read and Write. Each token gets its **own expiry**, chosen when the token is made (no default). Ticking Write also ticks Read. |
| 2026-10-30 | **Permanent IPs.** One field when blocking an IP: `permanent: true` never expires. Without it, the IP expires after the setup days (`ARCHIVE_AFTER_DAYS`). No per-IP days. |
| 2026-10-30 | **Same IP or domain again:** if it is in the list, nothing changes, whatever `permanent` says (no upgrade). If it is not in the list, it is added. |
| 2026-10-30 | **Two lists.** The EDL list is what is blocked now. The archive is a record of every different IP and domain ever blocked: written when it is first blocked, one row each, no duplicates, never removed. The row keeps first blocked, last blocked and times blocked. |
| 2026-10-30 | **Domains never expire** (domains are not dynamic like IPs). No `permanent` field for domains. Domains get the archive record too. |
| 2026-10-31 | **Docker, step 1 choices.** One container per server with everything inside (API, nginx, keepalived, timers, replication); a small built-in process manager replaces systemd. Replication in Docker goes **over HTTPS** (the leader sends the snapshot to the follower's Furnace with a replication secret), no SSH. The servers (`.deb`/`.rpm`) keep SSH replication for now. The first API token and the list login are made with `docker exec` commands and shown once, never in `docker logs`. The image comes as a file on the GitHub release (`docker load`) plus the Dockerfile in the repo; no registry. |
| 2026-11-01 | **Docker stays simple and separate (option a).** The container runs only Furnace; other software belongs in its own container or on the host. No custom keepalived file, no extra keepalived settings and no external failover mode in v0.8.0: the user only fills in `docker-compose.yml`. An external role command (`furnace role leader|follower`) may come with the Kubernetes step, where it is needed. |
| 2026-11-01 | **Bulk upload of blocked IPs and domains through the API** (changes the 2026-10-28 "no bulk block endpoint" decision). `POST /api/v1/ips/bulk` and `/api/v1/domains/bulk`, blocklist write, the same rules as `furnace import-ips` (dates, whitelist, archive, backup, all or nothing, `MAX_IPS`), `dry_run` and `permanent`. Still one token per program. |
| 2026-11-01 | **The servers replicate over HTTPS too, in v0.8.0** (option a). The leader sends the snapshot to `https://PEER_IP/replica/snapshot`; the secret is derived from the TLS key both servers already share, so an upgrade needs no new bundle. For one version the leader falls back to SSH when HTTPS does not work (an old follower during the upgrade). The version after v0.8.0 removes SSH replication. |
| 2026-11-01 | **No Docker image in v0.8.0.** The Docker work moves to the branch `docker` (kept, parked); `main` and the release have no Docker files or Docker docs. The shared parts (HTTPS replication, the notify script without systemd) stay. |
| 2026-11-01 | **`incident_id` is removed** (it can mean different things to different callers). A request that still sends it is refused (`400`). The upgrade deletes the stored values (schema 5); the columns stay empty so older followers and backups still fit. |

## Done

| Step | Version | What |
|---|---|---|
| 1 | v0.4.0 | Debian 12 and 13. Flask and gunicorn bundled. Python 3.9+. |
| 2 | v0.5.0 | `.rpm` for RHEL / Rocky / AlmaLinux / Oracle Linux / CentOS Stream 8, 9, 10, Fedora 41+, Amazon Linux 2023. firewalld. SELinux (tested on a real AlmaLinux 9.8 VM). |
| 3 | v0.6.0 | Token permissions: a grid of Blocklist / Whitelist and Read / Write (write includes read). Each token has its own expiry, chosen when it is made (no default). `token edit`, `rotate` keeps the days, last use, warnings in `status` and the nightly cleanup log. Setup makes one token and asks its name, boxes and days. Tokens of 0.5 keep their rights and never expire. Integration 108/108 (`.deb`) and 107/107 (`.rpm`) on all 11 systems. |
| 4 | v0.7.0 | Permanent IPs (`permanent: true` never expires; the same IP again changes nothing). The archive is a record of every different IP and domain ever blocked (first and last blocked, times blocked). Domains never expire. Schema 4 with a migration from 0.6. Unit tests 175. Integration 116/116 (`.deb`) and 115/115 (`.rpm`) on all 11 systems. |

## Next

### Later ideas (not planned)

- **Faster replication.** Today the leader sends a full snapshot every minute, so a dead leader can lose up to about
  one minute of changes. Options: a shorter interval (15 to 30 seconds, small change) or real-time replication
  (bigger, fits the high-availability phase).
- A UI with its own user logins. High availability with more servers.

### After v0.7.0: Docker support, both ways (user decision 2026-10-30)

Not in v0.8.0 (user decision 2026-11-01): step 1 is coded and tested (Docker test 48/48) on the branch `docker`, parked until the user continues it. Containers stay unsupported on `main`.

1. **Shared base + option A** (coded 2026-11-01 on the branch `docker`, Docker test 48/48, parked, not released):
   - One Furnace image (Debian 12 slim, the same bundled Flask and gunicorn). One container per server runs the
     API, nginx, keepalived and the timers. A small process manager (`furnace/container.py`) replaces systemd:
     it starts them, restarts what dies, runs replication every minute, backup at 01:30 and cleanup at 02:30
     (leader only), and stops everything cleanly.
   - Settings from environment variables (`FURNACE_ROLE`, `FURNACE_THIS_IP`, `FURNACE_PEER_IP`,
     `FURNACE_SHARED_IP`, ...) instead of setup questions. They are checked at start; a wrong value stops the
     container with a clear message. Two volumes: `/etc/furnace` (certificate and secrets) and
     `/var/lib/furnace` (database, lists, backups).
   - First start on the leader: certificate, keepalived password and replication secret are made, plus the
     follower bundle file. The follower starts only with that bundle in its `/etc/furnace` volume.
   - Replication over HTTPS: every minute the leader sends a gzip snapshot to `https://PEER_IP/replica/snapshot`
     with the replication secret. Only the peer IP may call it (nginx). The follower checks the secret, saves
     the snapshot and loads it. The servers (`.deb`/`.rpm`) keep SSH.
   - The first API token and the list login: `docker exec -it furnace furnace token add ...` and
     `furnace edl-user ...`. Secrets are shown once, never in `docker logs`.
   - No host firewall changes from inside a container (the README lists the ports to open).
   - Failover as today: keepalived on the host network (`NET_ADMIN`, `NET_RAW`), two hosts, one shared IP.
   - `docker/docker-compose.yml` example. A Docker test (`tests/docker/run.sh`): two containers without
     `--privileged`, a fake list reader and a fake caller, replication, failover and restart with the volumes.
   - Release: `furnace-image-0.8.0.tar.gz` (`docker load`) next to the `.deb` and `.rpm`.
2. **Option B: Kubernetes** (about 2 to 3 more sessions, with the high-availability phase):
   - The leader decision becomes pluggable: the role file from keepalived (today) or a Kubernetes lease.
   - Clients reach the leader through a Kubernetes Service.
   - Replication must follow the leader inside Kubernetes (the hard part); fits real-time replication.
   - Test cluster: for example a small k3s VM in Hyper-V, with failover tests.

## How to test

- Unit tests (WSL Ubuntu-22.04): `~/furnace-venv/bin/python -m pytest -q`
- Docker image (on the branch `docker`): `tests/docker/run.sh`
- Docker integration, one system at a time (the PC has little free memory):
  `tests/integration/run.sh ubuntu:24.04` (or `debian:12`, `almalinux:9`, `fedora:42`, `amazonlinux:2023`, ...).
  Docker Desktop must be running. A failure at the install step is often a short DNS problem in Docker: run again.
- Real SELinux: the AlmaLinux VM `omerta-alma9` (Hyper-V). Starting it and reading its IP needs admin PowerShell:
  `Start-VM omerta-alma9` and `(Get-VMNetworkAdapter -VMName omerta-alma9).IPAddresses`. Count SELinux denials in
  the raw `/var/log/audit/audit.log`, not with `ausearch -ts`. Put the VM back as it was afterwards.
- Release: build the `.deb` in WSL (`packaging/build-deb.sh dist`), the `.rpm` in an `almalinux:9` container
  (`packaging/build-rpm.sh`), the image with `docker build -f docker/Dockerfile --build-arg VERSION=V -t furnace:V .` and
  `docker save furnace:V | gzip > dist/furnace-image-V.tar.gz`, then `git tag`, `git push`, `gh release create` with the three files.
