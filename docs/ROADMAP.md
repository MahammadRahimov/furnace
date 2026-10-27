# Furnace roadmap

State on 2026-10-27: **v0.5.0 is released** (`.deb` and `.rpm`). This file holds the plan for the next steps,
so the work can continue later exactly where it stopped.

## Decisions so far

| Date | Decision |
|---|---|
| 2026-10-26 | Furnace is its own project. Private repo `MahammadRahimov/furnace`. EDL limit 50,000. Self-signed HTTPS certificate. |
| 2026-10-26 | A real tool: one package, then `sudo furnace setup`, which asks questions. |
| 2026-10-27 | No IP rules needed: the API is protected by tokens (hashed, with scopes, lockout), the lists by a login. IP limits are optional (`any` is the default). A UI will come later and needs its own user logins. |
| 2026-10-27 | Vendor-neutral: no XSOAR or Palo Alto in the product. They are only examples in the README (section 15). |
| 2026-10-27 | Run on "every Linux that companies use". **Option A:** Flask and gunicorn are bundled in the package (`packaging/vendor.lock`), not taken from the system. EPEL is allowed but not needed. |

## Done

| Step | Version | What |
|---|---|---|
| 1 | v0.4.0 | Debian 12 and 13. Flask and gunicorn bundled. Python 3.9+. |
| 2 | v0.5.0 | `.rpm` for RHEL / Rocky / AlmaLinux / Oracle Linux / CentOS Stream 8, 9, 10, Fedora 41+, Amazon Linux 2023. firewalld. SELinux (tested on a real AlmaLinux 9.8 VM). |

## Next: step 3, SUSE (about 2.5 hours)

Goal: openSUSE Leap 15.6 and 16.0, and SLES 15 / 16.

- A SUSE `.rpm`: other package names (for example `python311`, `openssh-clients` vs `openssh`), `zypper`.
  Either `%if 0%{?suse_version}` blocks in `packaging/rpm/furnace.spec`, or a second spec. Build it in an
  `opensuse/leap` container.
- `deploy/setup.sh`: a `suse` family. Check the nginx paths and user on SUSE (`/etc/nginx/vhosts.d/`? user and group
  `nginx`?), the SSH unit (`sshd`), firewalld (SUSE's default firewall).
- Security layer: Leap 15.6 / SLES 15 use **AppArmor**, Leap 16 / SLES 16 use **SELinux**. Check that nginx may read
  `/var/lib/furnace-edl` and `/var/lib/furnace-auth` under AppArmor, and that the SELinux part works there too.
- Tests: `tests/integration/run.sh opensuse/leap:15.6` and `opensuse/leap:16.0` (a third Dockerfile, or a zypper
  branch in `Dockerfile.rpm`).
- Limits to write in the README:
  - SLES itself cannot be tested without a paid subscription. Leap is built from the same packages.
  - On SLES, keepalived is only in the paid High Availability extension. Without it: one server, no shared IP.

## Then: step 4, generic `.tar.gz` (about 2 hours)

Goal: any other Linux with systemd (for example Arch Linux).

- `packaging/build-tarball.sh`: the same files as the packages, plus `install.sh` and `uninstall.sh`.
- `install.sh` checks that nginx, keepalived, Python 3.9+, openssl, curl, ssh, sqlite3 and systemd are there and
  says what is missing (it installs nothing itself). Then it copies the files and tells the user to run
  `sudo furnace setup`.
- `deploy/setup.sh`: a generic family (nginx `conf.d` or `sites-available`, whichever exists; nginx user from
  `nginx -V` or the config; firewall: firewalld, ufw or none).
- Tests: Arch Linux in Docker, and the tarball on one Debian system.
- Not covered: systems without systemd (Alpine, Devuan). Furnace uses systemd services and timers.

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
