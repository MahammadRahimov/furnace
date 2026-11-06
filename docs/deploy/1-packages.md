# Deployment guide 1: packages (.deb / .rpm)

Furnace is installed as a normal package on two Linux servers. `furnace setup` asks a few questions and sets up
everything: the API, nginx, keepalived, the host firewall, replication, the backup and the cleanup.

[Back to the overview](README.md)

## Before you start

- Two servers with a supported system, **the same one on both**:
  Ubuntu 22.04/24.04, Debian 12/13 (`.deb`), or AlmaLinux 8/9/10, Rocky 9, Oracle 9, Fedora 42,
  Amazon Linux 2023 (`.rpm`).
- The servers can reach their package mirror (apt or dnf installs nginx, keepalived and Python from it).
- Three IPs: leader `192.0.2.11`, follower `192.0.2.12`, shared IP `192.0.2.10` (examples).
- **keepalived on these servers is for Furnace only.** Setup writes the whole `/etc/keepalived/keepalived.conf`
  (an existing one is saved as `keepalived.conf.before-furnace.*`). If other services on the server use
  keepalived, use [guide 3](3-docker-host-keepalived.md) instead.
- Port 443 free (no other web server on it).
- The network allows: list reader and caller -> shared IP TCP 443; between the servers TCP 443 and VRRP
  (IP protocol 112). Setup opens these on the **host** firewall itself. During an upgrade from 0.9 or older, also
  SSH (TCP 22) between the servers.

## Step 1. Get the package (both servers)

From the GitHub releases page <https://github.com/MahammadRahimov/furnace/releases>:

```bash
R=https://github.com/MahammadRahimov/furnace/releases/download/v0.10.0
curl -fLO $R/furnace_0.10.0_all.deb              # Ubuntu, Debian
curl -fLO $R/furnace-0.10.0-1.noarch.rpm         # Red Hat family, Fedora, Amazon Linux
```

Or build it: `packaging/build-deb.sh` or `packaging/build-rpm.sh` (the file lands in `dist/`).

## Step 2. Leader (server A, 192.0.2.11)

```bash
sudo apt install ./furnace_0.10.0_all.deb        # or: sudo dnf install ./furnace-0.10.0-1.noarch.rpm
sudo furnace setup
```

Answer the questions. Press Enter to take the answer in [brackets]:

```
Is this server the leader or the follower? [leader]:
Network card name [eth0]:                          <- the card that has 192.0.2.11 (see: ip -br addr)
This server's IP (the leader) [192.0.2.11]:
The follower server's IP: 192.0.2.12
Shared IP: 192.0.2.10
Network prefix of the shared IP (24 = 255.255.255.0) [24]:
Who may call the API [any]:                        <- or the caller's IPs
Who may download the lists [any]:                  <- or the list reader's IPs
Most entries per list [50000]:
Days an IP stays blocked (permanent IPs never expire) [90]:
VRRP router ID (1-255) [51]:                       <- must be unused by other keepalived on this network
SSH allowed from [any]:
Let Furnace set up this server's own firewall (ufw)? [yes]:
Token name [api]: soar
... the permission grid, then: Token expires after how many days: 365
Set up Furnace with these settings? [yes]:
```

Setup configures and starts everything and runs a self-test. At the end it shows **two secrets once**:

- the **API token** (for the caller, for example the SOAR),
- the **list login** (user `lists` and a password, for the list reader, for example the firewall).

Store them now. Furnace keeps only hashes and cannot show them again.

**Host firewall.** Setup uses the firewall that is **active** (ufw or firewalld). If both are active it stops and
explains; if none is active it asks before it turns one on.

## Step 3. Make a join code (on the leader)

```bash
sudo furnace join-code
```

It shows a code like `FJ1-ABCD-EFGH-IJKL-MNOP-QRST-UVWX-YZ23-4567`. The code works **once**, for **1 hour**, and
only from the follower's IP. 5 wrong tries remove it. A new code replaces the old one.

## Step 4. Follower (server B, 192.0.2.12)

```bash
sudo apt install ./furnace_0.10.0_all.deb
sudo furnace setup
```

```
Is this server the leader or the follower? [leader]: follower
Join code from the leader (Enter = use the file): FJ1-ABCD-EFGH-IJKL-MNOP-QRST-UVWX-YZ23-4567
Network card name [eth0]:
```

The follower checks the leader's certificate against the code, then fetches the certificate and secrets over
HTTPS. It takes all shared settings from the leader, so it asks only for the network card (and the firewall).

**No join code possible** (for example port 443 on the leader is not reachable yet)? Copy the bundle file instead:

```bash
# on the leader
sudo scp /root/furnace-peer-bundle.tar.gz you@192.0.2.12:~/
# on the follower: run setup, answer follower, press Enter at the join code question
sudo furnace setup
```

Setup finds the bundle in your home folder, `/root` or `/tmp`, uses it and deletes it.

## Step 5. Check

```bash
sudo furnace status                # on both: role, services, replication
sudo furnace failover-test         # on the server with the shared IP: the other one takes over and stays leader
```

From another machine:

```bash
curl -k -u lists:PASSWORD https://192.0.2.10/edl/blocklist.txt     # an empty list at first
```

## Step 6. Connect the list reader and the caller

- The list reader downloads `https://192.0.2.10/edl/blocklist.txt` (also `domains.txt`, `urls.txt`) with the list
  login.
- The caller sends API calls to `https://192.0.2.10/api/v1/...` with the token.
- Both should trust the certificate `/etc/furnace/tls/furnace.crt` (it is public).

Details: README [section 5](../../README.md#5-checklist-of-manual-work) and [section 6](../../README.md#6-api-guide).

## Without questions (automation)

The same settings file works on **both** servers:

```bash
cp /usr/share/doc/furnace/config.env.example config.env
nano config.env
```

```
LEADER_IP=192.0.2.11
FOLLOWER_IP=192.0.2.12
SHARED_IP=192.0.2.10
#JOIN_CODE=FJ1-...        # follower only; used once, never saved
```

```bash
sudo furnace setup --check --config config.env    # shows what it would do, changes nothing
sudo furnace setup --config config.env
```

## Daily work, upgrade, uninstall

- Daily work: `sudo furnace status`, `sudo furnace token add NAME`, `sudo furnace edl-user add NAME`
  (README [section 7](../../README.md#7-daily-admin-work-the-furnace-command)).
- Upgrade: install the new package, **follower first**, then the leader (`sudo apt install ./NEW.deb`). The package runs
  `furnace setup --upgrade` by itself: no questions, and your answers, data and secrets are kept. An upgrade from
  0.9 saves the settings with the new IP names; nothing to do.
- Change a setting: `sudo furnace setup` again (the brackets show the current answers).
- Uninstall: README [section 12](../../README.md#12-upgrade-and-uninstall).
