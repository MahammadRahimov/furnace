# Next release: v0.10.0 (not released)

What changes in the next release, compared with v0.9.0.

How a release happens (user decision 2026-11-01):

1. Every finished change is added to this list. The lab can be updated and tried at any time.
2. Nothing is released until the user says "update" or "new version".
3. Then: the full tests (unit tests, the 11-system integration round, the package tests and the Docker tests),
   then the version on GitHub and the release with the packages, the Docker image and the compose files.
4. After the release, this list is emptied for the version after it.

## What changes

### 1. Setup configures the host firewall that is active (done 2026-11-06, tests passed)

The package (`furnace setup`, `.deb` and `.rpm`) now sets up the host firewall that is **active**, not the one that
is only installed. Before, it picked ufw on Ubuntu whenever ufw was installed, so on an Ubuntu server that runs
firewalld it turned on ufw as a second firewall. Docker is not changed (the container never touches the host
firewall).

- **Active** means running now, or switched on for the next boot. (Starting firewalld wipes ufw's rules from the
  kernel, so `ufw status` says inactive while ufw is still on and loads again at the next boot.)
- firewalld active: firewalld, also on Ubuntu. ufw active: ufw.
- **Both active: setup stops** and explains how to turn one off. Every packet must pass both, so ports opened in
  one are not enough. The questions offer to go on without the host firewall (default no); a settings file can
  say `HOST_FIREWALL=none`. Then setup warns that both are on and it changed neither.
- **None active:** setup asks before it turns the installed one on: "Turn on and set up this server's own
  firewall (ufw)?" SSH is allowed first. The follower asks this too (before, it only took the leader's answer).
  A settings file turns it on only when it has `HOST_FIREWALL`; without it, setup leaves the firewall alone and
  says how to let it turn it on.
- `HOST_FIREWALL` in a settings file must match the active firewall (`ufw` while firewalld is the active one is
  refused).
- **A package upgrade never stops for the firewall and never turns one on.** Both on, the active one changed, or
  the firewall turned off: it leaves the firewall alone and says why.
- `furnace setup --check` shows which firewall is active and whether setup would turn one on.
- New test `tests/integration/firewall.sh` (Debian family, ufw and firewalld side by side).

### 2. Clearer IP settings: the same file on both hosts (done 2026-11-06, tests passed)

`THIS_IP`/`PEER_IP` ("me"/"the other one") had to be swapped on the follower, which was easy to get wrong. The new
names say which host is which, so the settings file is **the same on both hosts**:

- `LEADER_IP`, `FOLLOWER_IP`, `SHARED_IP` (Docker: `FURNACE_LEADER_IP`, `FURNACE_FOLLOWER_IP`, `FURNACE_SHARED_IP`).
- Furnace sees which of the two IPs is on this host's network card and so knows its role. `ROLE` is needed only
  when both IPs are on the same host (a test setup).
- Clear messages when neither IP is on this host, when `ROLE` does not match the IP, when both IPs are the same,
  and when old and new names are mixed.
- At start it says: "This host is the leader (…); the other host is …".
- Old files with `THIS_IP`/`PEER_IP` keep working, with a note to switch. `furnace setup` saves the new names
  (also after an upgrade from 0.9.0).

### 3. Join code: the follower fetches the certificate and secrets itself (done 2026-11-06, tests passed)

No more copying `furnace-peer-bundle.tar.gz` by hand. Docker and the packages.

- On the leader: `furnace join-code` (Docker: `docker exec furnace furnace join-code`) prints a one-time code.
- On the follower: put it in `.env` as `FURNACE_JOIN_CODE=...` (Docker), in the settings file as `JOIN_CODE=`, or
  type it at the setup question "Join code from the leader". The follower fetches everything over HTTPS.
- The code works **once**, for **1 hour**, and **only from the follower's IP**. 5 wrong tries remove it.
- Part of the code is the leader certificate's fingerprint. The follower checks it **before** it sends anything,
  so a fake leader gets nothing. The secrets travel encrypted with a key from the code.
- The code is never saved in `setup.conf`. `furnace setup --check` does not use it.
- The bundle file still works as a fallback.

### 4. Docker: use the host's own keepalived (done 2026-11-06, tests passed)

For hosts that already run keepalived for other services. `FURNACE_KEEPALIVED=external` in `.env`:

- The container runs no keepalived and needs no `NET_ADMIN`/`NET_RAW`.
- Furnace gets its **own** `vrrp_instance` (its own shared IP and router ID) next to the host's other services.
- `docker exec furnace furnace host-keepalived` prints the steps; `... host-keepalived config`, `notify` and
  `health-check` print the block and the two small scripts. You paste them; Furnace never edits
  `keepalived.conf`.
- The host's keepalived writes the role into `/run/furnace`, which the container reads.
  `furnace status` warns when no role has come from the host yet.
- The built-in keepalived stays the default.

**Tests for 2–4:** 359 unit tests; Docker test 63/63; new Docker test with keepalived on the host 31/31; the
integration round 176/176 on Ubuntu 22.04/24.04 and Debian 12/13 and 175/175 on AlmaLinux 8/9/10, Rocky 9,
Oracle 9, Fedora 42 and Amazon Linux 2023; the firewall test 46/46 on 4 systems; the two real lab VMs 44/44
(upgrade from 0.9.0 with the same `.env` on both, follower rejoins with a join code over the real network,
keepalived on the hosts with another service's shared IP that does not move when Furnace fails over).

### 5. Deployment guides (done 2026-11-07)

New `docs/deploy/`: one step-by-step guide for each way to deploy: packages (`.deb`/`.rpm`), Docker with
keepalived in the container, and Docker with the host's own keepalived. Each guide has the order of the steps, the
ports to open and the failover test. Linked from the README.

### 6. SSH replication becomes an optional backup, off by default (done 2026-11-07, tests passed)

Decided by the user 2026-11-07 (replaces the 2026-11-01 decision to remove SSH replication). Packages only;
Docker never used SSH.

- **The copy to the other server always goes over HTTPS** (port 443, every 20 seconds), as before.
- **SSH is an optional backup:** setup asks "Use SSH as a backup for the copy to the other server?" (default no);
  in a settings file `REPLICATION_SSH_FALLBACK=yes`. With it on, the leader copies over SSH only when HTTPS does
  not work, and `furnace status` warns: "HTTPS does not work: the copy went over the SSH backup".
- **With the backup off** (the default) there is no `furnace-sync` user, no SSH login for it, and no SSH rule
  between the servers in the host firewall. Turning it off later removes them. When the copy fails,
  `furnace status` says what to check and that the SSH backup is off.
- **Plain HTTP is removed.** `USE_HTTPS=no` (a test setting) is refused with a clear message; an upgrade switches
  it to HTTPS with a note.
- The admin's own SSH rule (`SSH allowed from`) stays as it is.
- **Upgrade from 0.9:** SSH is turned off and `furnace-sync`, its login and its firewall rule are removed.
  **Upgrade from 0.7 or older** (no HTTPS secret yet): the SSH backup stays on, with a note to turn it off once
  HTTPS works.

**Tests:** 359 unit tests; the integration round 199/199 on Ubuntu 22.04/24.04 and Debian 12/13 and 198/198 on
AlmaLinux 8/9/10, Rocky 9, Oracle 9, Fedora 42 and Amazon Linux 2023 (backup off by default, HTTPS broken with
the backup off and on, a simulated 0.7 upgrade, turning it off, the 0.9 upgrade clean-up, `USE_HTTPS=no`
refused); the firewall test 46/46 on 4 systems; Docker test 63/63; Docker with keepalived on the host 31/31.

### Decided, not wanted

- Your own certificate (bring your own TLS certificate): not wanted (user, 2026-11-07).
