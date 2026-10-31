# Furnace roadmap

State on 2026-10-31: **v0.7.0 is released** (`.deb` and `.rpm`). Next: Docker support, step 1. This file holds the plan for the next steps,
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

Starts after the v0.7.0 test round and release. Containers are not supported until then (the README says so).

1. **Shared base + option A** (about 2 to 3 sessions, released on its own):
   - An official Furnace image: API, nginx and the timers without systemd.
   - Settings from environment variables instead of setup questions. Volumes for the database and the certificate.
   - No host firewall changes from inside a container.
   - Failover as today: keepalived on the host network (`NET_ADMIN`), two hosts, one shared IP, SSH snapshots.
   - A `docker-compose.yml` example. Tests for the image.
2. **Option B: Kubernetes** (about 2 to 3 more sessions, with the high-availability phase):
   - The leader decision becomes pluggable: the role file from keepalived (today) or a Kubernetes lease.
   - Clients reach the leader through a Kubernetes Service.
   - Replication must follow the leader inside Kubernetes (the hard part); fits real-time replication.
   - Test cluster: for example a small k3s VM in Hyper-V, with failover tests.

## How to test

- Unit tests (WSL Ubuntu-22.04): `~/furnace-venv/bin/python -m pytest -q`
- Docker integration, one system at a time (the PC has little free memory):
  `tests/integration/run.sh ubuntu:24.04` (or `debian:12`, `almalinux:9`, `fedora:42`, `amazonlinux:2023`, ...).
  Docker Desktop must be running. A failure at the install step is often a short DNS problem in Docker: run again.
- Real SELinux: the AlmaLinux VM `omerta-alma9` (Hyper-V). Starting it and reading its IP needs admin PowerShell:
  `Start-VM omerta-alma9` and `(Get-VMNetworkAdapter -VMName omerta-alma9).IPAddresses`. Count SELinux denials in
  the raw `/var/log/audit/audit.log`, not with `ausearch -ts`. Put the VM back as it was afterwards.
- Release: build the `.deb` in WSL (`packaging/build-deb.sh dist`), the `.rpm` in an `almalinux:9` container
  (`packaging/build-rpm.sh`), then `git tag`, `git push`, `gh release create` with both files.
