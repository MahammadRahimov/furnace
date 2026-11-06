# Furnace roadmap

State on 2026-11-04: **v0.9.0 is released** (`.deb`, `.rpm` and the Docker image with its compose files). The list for the next version is in `docs/NEXT-RELEASE.md`. Next: the SSH removal. This file holds the plan for the next steps,
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
| 2026-11-02 | **A URL blocklist (option B), in v0.8.0.** Its own list type: API, bulk file, `/edl/urls.txt`, archive record, replication; the blocklist permission (like domains). URLs **never expire**. `http://`/`https://` are removed so the firewall always gets a working line. **Wildcards** (`*`, `^`, whole parts) are allowed, too wide ones are refused (`*.com/`, `*.co.uk/`). **The whitelist protects by host:** the existing domain whitelist (no separate URL whitelist), and a whitelisted domain also protects its subdomains (`x.com` on the whitelist: no `x.com/...`, `www.x.com/...` or `*.x.com/`). URL hosts that are IPs use the IP whitelist. |
| 2026-11-03 | **Export and upload again**, in v0.8.0: `GET /api/v1/export/<list>` for the active lists, the three archives and the whitelist. The export is a Furnace file (marker line `# furnace-export <list> v1`) that the bulk uploads and imports read back by column name, keeping dates, `added_by`, reason, permanent and the archive history, so the lists move to a new server. Tokens, logins, the certificate and the audit log are not exported. |
| 2026-11-03 | **Certificate fix for RHEL 8**, in v0.8.0: setup makes the certificate from its own OpenSSL settings file (no repeated extension), the replication trusts the shared certificate directly so old certificates work, and `furnace status` warns about an old one. The certificate is never replaced on its own (the firewall must get the new one too). |
| 2026-11-03 | **Check the whitelist**, in v0.8.0: `GET /api/v1/whitelist/check/<ip or domain>` (whitelist read only, private IPs allowed) answers `whitelisted` and the entry that protects the value. **URL wildcards may not cover a shared platform** (Public Suffix List): `*.github.io/`, `*.amazonaws.com/` are refused. |
| 2026-11-02 | **The domain whitelist protects subdomains everywhere** (domains too, not only URLs): whitelisting `x.com` removes and refuses `mail.x.com`. **The whitelist is restricted:** general sites where anyone can make a site cannot be whitelisted. Identified with the **Public Suffix List** (bundled, updated each release; shared platforms like `github.io` and public names like `co.uk`, and names that cover one like `amazonaws.com`), plus an optional local file `/etc/furnace/whitelist-refuse.txt`. The upgrade cleans existing data once; old refused entries stay with a warning. IPs unchanged (ranges already work this way). |
| 2026-11-01 | **`incident_id` is removed** (it can mean different things to different callers). A request that still sends it is refused (`400`). The upgrade deletes the stored values (schema 5); the columns stay empty so older followers and backups still fit. |
| 2026-11-04 | **Docker comes first in v0.9.0**, before the SSH removal. Goal: anyone can deploy the leader and the follower as separate containers. **Two ways are supported:** (1) **two hosts, one container each**, for real use (keepalived on the host network moves the shared IP: real failover); (2) **one host, both containers** (one Compose file, private Docker network), **for testing and learning only** (no real failover: one host). **Kubernetes later**, only if someone asks (step 2 below). **Configuration the standard way:** Furnace ships `docker-compose.yml` (never edited) and an example `.env`; the admin edits only `.env` (role and IPs). Leader and follower use the same compose file, only `.env` differs. Optional later: an `init` command that asks the questions and writes `.env`. |
| 2026-11-05/06 | **The package sets up the ACTIVE host firewall** (v0.10.0). firewalld running -> firewalld (also on Ubuntu); ufw active -> ufw; **both active -> setup stops and explains** (going on without the host firewall is possible, default no); none active -> the installed one, **asked before it is turned on**. Active = running now or switched on for the next boot. A package upgrade never stops for it and never turns a firewall on. Docker unchanged. |
| 2026-11-06 | v0.10.0, decided (done and tested the same day): **(A)** IP settings `LEADER_IP`/`FOLLOWER_IP`/`SHARED_IP`, the same file on both hosts, role found from the IP on this host (old `THIS_IP`/`PEER_IP` still work); **(B)** a one-time **join code** (1 hour, once, only from the follower's IP, carries the leader certificate fingerprint) replaces copying the bundle, bundle stays as fallback; Docker and packages; **(C)** Docker **external keepalived** mode (`FURNACE_KEEPALIVED=external`, own vrrp_instance on the host, Furnace prints the block, never edits keepalived.conf, built-in stays default). |
| 2026-11-07 | v0.10.0: **SSH replication stays as an optional backup, off by default** (replaces "remove SSH" of 2026-11-01). HTTPS always; `REPLICATION_SSH_FALLBACK=yes` adds the SSH copy when HTTPS fails, `furnace status` warns. Plain HTTP (`USE_HTTPS=no`) removed. The admin's SSH rule stays. Upgrade from 0.9 turns SSH off and removes `furnace-sync`; from 0.7 SSH stays on with a note. No own-certificate feature (not wanted). |

## Done

| Step | Version | What |
|---|---|---|
| 1 | v0.4.0 | Debian 12 and 13. Flask and gunicorn bundled. Python 3.9+. |
| 2 | v0.5.0 | `.rpm` for RHEL / Rocky / AlmaLinux / Oracle Linux / CentOS Stream 8, 9, 10, Fedora 41+, Amazon Linux 2023. firewalld. SELinux (tested on a real AlmaLinux 9.8 VM). |
| 3 | v0.6.0 | Token permissions: a grid of Blocklist / Whitelist and Read / Write (write includes read). Each token has its own expiry, chosen when it is made (no default). `token edit`, `rotate` keeps the days, last use, warnings in `status` and the nightly cleanup log. Setup makes one token and asks its name, boxes and days. Tokens of 0.5 keep their rights and never expire. Integration 108/108 (`.deb`) and 107/107 (`.rpm`) on all 11 systems. |
| 4 | v0.7.0 | Permanent IPs (`permanent: true` never expires; the same IP again changes nothing). The archive is a record of every different IP and domain ever blocked (first and last blocked, times blocked). Domains never expire. Schema 4 with a migration from 0.6. Unit tests 175. Integration 116/116 (`.deb`) and 115/115 (`.rpm`) on all 11 systems. |
| 5 | v0.8.0 | Blocklist files through the API, the URL blocklist, replication over HTTPS every 20 seconds (SSH fallback), the domain whitelist protects subdomains and refuses shared platforms (Public Suffix List), the whitelist check, export and upload again, `incident_id` removed, 90 days by default, keepalived crash and expired-certificate fixes, the RHEL 8 certificate fix. Unit tests 323. Integration 159/159 (`.deb`) and 158/158 (`.rpm`) on all 11 systems. |
| 6 | v0.9.0 | Docker: one image for the leader and the follower on two hosts (`docker-compose.yml` + `.env`, keepalived on the host network, HTTPS replication every 20 seconds, a built-in process manager), a one-host test lab, the image and the compose files on the release. Unit tests 338 (Python 3.9, 3.10, 3.13). Integration 159/159 (`.deb`) and 158/158 (`.rpm`) on all 11 systems. Docker 56/56 and lab 18/18; two Hyper-V VMs: failover, host down, API 120/120. |

## Next

### Later ideas (not planned)

- **Real-time replication** (high-availability phase, user decision 2026-11-02). v0.8.0 sends the full snapshot
  every 20 seconds (was 1 minute), so a dead leader can lose up to about 20 seconds of changes. Plan for later:
  a change log on the leader (every write numbered, in the same transaction), the follower asks for "changes after
  N" over the HTTPS replication, applies them in order, falls back to the full snapshot when it is too far behind;
  `furnace status` shows the lag. Async first (sync = the leader waits for the follower, slower writes). Leader
  terms so a returning old leader drops changes the new leader never got. Still 2 servers.
- **Witness / tie-breaker** (later, with the HA phase). With 2 servers, a broken link between them makes both
  leader (split brain). A small third vote with no data (a tiny VM, or "can I reach the gateway") fixes most of it
  without a third Furnace server.
- A UI with its own user logins. High availability with more servers.

### After v0.7.0: Docker support, both ways (user decision 2026-10-30)

Not in v0.8.0 (user decision 2026-11-01): step 1 is coded and tested (Docker test 48/48) on the branch `docker`, parked until the user continues it. Containers stay unsupported on `main`.

**Continued for v0.9.0 (user decision 2026-11-04), released in v0.9.0 on 2026-11-04.** The Docker files
came back from 376477a and follow `main` now: replication every 20 seconds, the Public Suffix List in the image,
`whitelist-refuse.txt`, the URL list, 90 days, the health check with `-k`. New: `docker/.env.example` (the only
file the admin edits; the compose file reads it with `env_file`), `docker/docker-compose.lab.yml` (one host,
leader + follower + client on 203.0.113.0/24, test only), README 3.6 and 3.7, `tests/docker/lab.sh` (the README
lab steps). Tests: unit 337, `tests/docker/run.sh` 56/56 (now on 198.51.100.0/24, so it runs next to the lab),
`tests/docker/lab.sh` 18/18. **Two-VM test PASSED 2026-11-04** (Hyper-V, Ubuntu 24.04, Docker 29, the README 3.6 steps,
`network_mode: host`): docker stop on the leader 40/40 list downloads OK; hard crash of the leader VM (sysrq reboot)
44/45 (one failure, about 3 s), no data lost, the crashed host restarted Furnace by itself as the standby. Host-down
test (power off, host stays off): leader host off 17 min, the other served everything, the returning host caught
up within 20 s as the standby; the reverse (new leader host off) and follower host off: 33/33. Fixed after it: the
container logged "replication FAILED" every 20 s while a host was down; now the first failure, a reminder every
10 minutes and "works again after N failed tries". The upgrade (new image + docker compose up -d) kept the roles. Full API test
on the two VMs (furnace-lab/docker-vm/api-test.sh): every API route and every furnace command through the shared
IP, then all 8 lists the same on both hosts: 120/120. It found
one README bug: `./config` is root-only, so the bundle is taken out with `docker cp` (fixed). Left for the
release: the image file `furnace-image-0.9.0.tar.gz`.

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
   - Release: `furnace-image-V.tar.gz` (`docker load`), `docker-compose.yml`, `.env.example` and
     `docker-compose.lab.yml` next to the `.deb` and `.rpm`.
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
- Host firewall choice (Debian family, ufw and firewalld side by side): `tests/integration/firewall.sh ubuntu:24.04`
  (or `ubuntu:22.04`, `debian:12`, `debian:13`). Uses 203.0.113.0/24 (`NET_PREFIX=` to change).
- Real SELinux: the AlmaLinux VM `omerta-alma9` (Hyper-V). Starting it and reading its IP needs admin PowerShell:
  `Start-VM omerta-alma9` and `(Get-VMNetworkAdapter -VMName omerta-alma9).IPAddresses`. Count SELinux denials in
  the raw `/var/log/audit/audit.log`, not with `ausearch -ts`. Put the VM back as it was afterwards.
- Release: build the `.deb` in WSL (`packaging/build-deb.sh dist`), the `.rpm` in an `almalinux:9` container
  (`packaging/build-rpm.sh`), the image with `docker build -f docker/Dockerfile --build-arg VERSION=V -t furnace:V .` and
  `docker save furnace:V | gzip > dist/furnace-image-V.tar.gz`, then `git tag`, `git push`, `gh release create` with
  **all of these files** (user decision 2026-11-04: Docker is important, everything for it goes on the release):
  the `.deb`, the `.rpm`, `furnace-image-V.tar.gz`, `docker-compose.yml`, `.env.example` (upload it as
  `env.example`: GitHub drops a leading dot) and `docker-compose.lab.yml`. The release notes get a Docker section
  (how to load the image, the two files, README 3.6 and 3.7). Before the release: the Docker tests
  (`tests/docker/run.sh`, `tests/docker/lab.sh`) and the two-VM API test (`furnace-lab/docker-vm/api-test.sh`).
