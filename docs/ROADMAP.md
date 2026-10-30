# Furnace roadmap

State on 2026-10-30: **v0.6.0 is released** (`.deb` and `.rpm`). v0.7.0 is planned below. This file holds the plan for the next steps,
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

## Next

### v0.7.0: permanent IPs and the archive record (planned, waiting for the user's go)

IPs:

- `POST /api/v1/ips` gets an optional field `permanent` (true or false, default false).
- Temporary IPs leave the EDL list after `ARCHIVE_AFTER_DAYS` (nightly cleanup, as today). Permanent IPs never leave
  it. Only an unblock removes them.
- The same IP again while it is in the list: `200 already_exists`, nothing changes, `permanent` is ignored.
- Look-ups show `permanent` and, for temporary IPs, the expiry date.
- `import-ips` and the bulk upload get the same option (`--permanent`).
- `furnace status` shows how many IPs are permanent and warns when they take a large part of the 50,000 limit.

Domains:

- Never expire, as today. No `permanent` field.

Archive record (IPs and domains):

- Written when an IP or domain is blocked for the first time, at the same time as the EDL list.
- One row per IP or domain, never a duplicate, never removed. It keeps the first details (date, reason, added by,
  incident) and also last blocked and times blocked.
- Expiry and unblock remove it only from the EDL list. Blocked again later: back in the EDL list, the record row
  gets a new last blocked and times blocked + 1.

Upgrade from 0.6:

- New database schema with a migration. Every IP and domain in the EDL list now gets a record row. The old archived
  IPs become record rows too.
- IPs already in the list stay temporary with their current date. Domains already in the list stay as they are
  (they never expire).
- `docs/API.md` and the README are updated with it.

### Later ideas (not planned)

- **Faster replication.** Today the leader sends a full snapshot every minute, so a dead leader can lose up to about
  one minute of changes. Options: a shorter interval (15 to 30 seconds, small change) or real-time replication
  (bigger, fits the high-availability phase).
- A UI with its own user logins. High availability with more servers.

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
