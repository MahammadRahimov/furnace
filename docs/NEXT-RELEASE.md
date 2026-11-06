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

### Decided, not started

Already decided (user decision 2026-11-01): SSH replication is removed (HTTPS only). Not started.

Decided 2026-11-06 (user), being built now (order: A, B, C):

**A. Clearer IP settings: the same file on both hosts.** `THIS_IP`/`PEER_IP` ("me"/"the other one") had to be
swapped on the follower, which is easy to get wrong. New names: `LEADER_IP`, `FOLLOWER_IP`, `SHARED_IP`. The file
is the same on both hosts; Furnace sees which of the two IPs is on its network card and so knows its role (no
`ROLE` line needed). Neither IP on this host: it stops with a clear message. Old files with `THIS_IP`/`PEER_IP`
keep working, with a note to switch. Docker (`FURNACE_LEADER_IP`, ...) and the packages use the same names.

**B. Join code instead of copying the bundle file.** The leader shows a one-time code
(`furnace join-code`); the follower gets it in `.env` (`FURNACE_JOIN_CODE`) or in a setup question, and fetches
the certificate and secrets from the leader over HTTPS itself. The code works once, for 1 hour, only from the
follower's IP; 5 wrong codes stop it; part of the code is the leader certificate's fingerprint, so the follower
checks it talks to the real leader. The bundle file stays as a fallback. Docker and the packages.

**C. External keepalived mode (Docker).** `FURNACE_KEEPALIVED=external`: the container does not run keepalived
and needs no `NET_ADMIN`/`NET_RAW`; the host's own keepalived owns the shared IP. Furnace gets its own
`vrrp_instance` (own shared IP, own router ID) next to the host's other services. Furnace prints the block and
the two small scripts (notify, health check); the user pastes them, Furnace never edits `keepalived.conf`. Host
network stays. The host writes the role file into a folder shared with the container. The built-in keepalived
stays the default.
