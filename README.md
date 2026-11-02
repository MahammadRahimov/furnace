# Furnace

Furnace is a standalone blocklist server. It does not depend on one vendor.

A **caller** sends an IP address or a domain to the Furnace API (Application Programming Interface). The caller can
be a SOAR platform (Security Orchestration, Automation and Response), a script, or a UI. Furnace saves the entry and
publishes it in a plain text list, one entry per line.
A **list reader**, usually a firewall, downloads the list again and again and blocks what is in it. Firewalls give
these lists different names: EDL (External Dynamic List) on Palo Alto, threat feed on Fortinet, and so on.

Furnace keeps working when the caller is down. Only adding and removing stops.
The firewall keeps downloading the lists from Furnace.

The tested setup is Cortex XSOAR as the caller and a Palo Alto firewall as the list reader. The steps for them are
in [section 15](#15-examples-palo-alto-and-cortex-xsoar). Other products work the same way: any caller that can send
HTTPS requests, and any list reader that can download a text file over HTTPS with a user and password.

## Contents

1. [How it works](#1-how-it-works)
2. [What you need](#2-what-you-need)
3. [Install](#3-install)
4. [Setup questions and settings](#4-setup-questions-and-settings)
5. [Checklist of manual work](#5-checklist-of-manual-work)
6. [API guide](#6-api-guide)
7. [Daily admin work (the furnace command)](#7-daily-admin-work-the-furnace-command)
8. [Rules for IPs, domains and the whitelist](#8-rules-for-ips-domains-and-the-whitelist)
9. [The nightly cleanup and the archive](#9-the-nightly-cleanup-and-the-archive)
10. [Two servers: leader and follower](#10-two-servers-leader-and-follower)
11. [Files and folders](#11-files-and-folders)
12. [Upgrade and uninstall](#12-upgrade-and-uninstall)
13. [Tests](#13-tests)
14. [What was not tested](#14-what-was-not-tested)
15. [Examples: Palo Alto and Cortex XSOAR](#15-examples-palo-alto-and-cortex-xsoar)

The plan for the next versions (Kubernetes, high availability) is in [docs/ROADMAP.md](docs/ROADMAP.md).

---

## 1. How it works

```
 Analyst --> caller (SOAR, script, UI) --HTTPS API call + token--> Furnace (shared IP) --> SQLite database
                                                                          |
                                                                          +--> blocklist.txt  (IPs)
                                                                          +--> domains.txt    (domains)
                                                                                    ^
 List reader (firewall) --downloads every few minutes over HTTPS + login------------+
```

- The **database** (SQLite) is the master record.
- The two text files are always rebuilt from the database after each change.
- A new file is first written to a temporary file, then renamed. The firewall never sees half a file.
- A file lock makes sure two changes at the same time cannot clash.
- Every change is one database transaction. Every action is written to an audit log, also refused requests.
- Two servers share one IP address. If the leader fails, the follower takes over the shared IP.
- **Security:** every API call needs a token. The lists need a login (user and password). Both work from any
  address, so no IP rules are needed. You can still limit the addresses if you want (section 4).
  Furnace stores only hashes of tokens and passwords. An address that sends 10 wrong tokens is locked for
  15 minutes.

Technology: Linux with systemd, Python 3.9 or newer, Flask and gunicorn (bundled inside the package), SQLite in
WAL mode (Write-Ahead Logging) with `synchronous=FULL`, nginx, keepalived and OpenSSL.

**Supported systems** (tested, see [section 13](#13-tests)):

| System | Versions | Package |
|---|---|---|
| Ubuntu | 22.04, 24.04 | `.deb` |
| Debian | 12, 13 | `.deb` |
| RHEL, Rocky Linux, AlmaLinux, Oracle Linux, CentOS Stream | 8, 9, 10 | `.rpm` |
| Fedora | 41 and newer | `.rpm` |
| Amazon Linux | 2023 | `.rpm` |

SUSE is not supported (the work is parked on the branch `suse`). Both servers should run the same system.

**Docker and other containers are not supported.** Run Furnace on virtual machines or real servers. Furnace needs
systemd (its services and timers), keepalived with real network access (the shared IP must be reachable by the
list reader and the caller, and must move between two hosts), and its own host firewall rules. A normal container
has none of these, and data in a container without volumes is lost when the container is removed. The integration
tests do run in containers, but only with `--privileged` and systemd, on one host: that is fine for a test, not for
real use (almost full control of the host, and no failover when the host dies). Container support is planned
(`docs/ROADMAP.md`); the work so far is parked on the branch `docker`.

| | Debian family (`.deb`) | Red Hat family (`.rpm`) |
|---|---|---|
| Host firewall | ufw | firewalld |
| nginx site | `/etc/nginx/sites-available/furnace` | `/etc/nginx/conf.d/furnace.conf` |
| Security layer | AppArmor (nothing to do) | SELinux: setup sets the labels (section 3.5) |
| Python | the system's `python3` | `python3` (3.9+), or `python3.11` / `python3.12` on RHEL 8 (installed by dnf) |

**Bundled libraries.** Flask, gunicorn and the small libraries they need are inside the package
(`/usr/lib/furnace/vendor`), with exact versions checked by SHA-256 (`packaging/vendor.lock`). So Furnace runs the
same on every system, and the servers never download anything from PyPI (the Python Package Index). The other side:
a security fix in Flask or gunicorn reaches you with a new Furnace release, not with the system's own updates.

## 2. What you need

- Two Linux servers from the supported list (above), in the same network segment. One is enough for a lab.
  Both should run the same system.
- Three IP addresses in that segment: one per server and one **shared IP** (VIP, virtual IP).
- The servers need their system's package mirror (directly, through a proxy, or a local mirror). apt installs
  nginx, keepalived, Python and a few tools from there. Flask and gunicorn are already in the Furnace package.
- Optional: the IP addresses of the list reader and of the caller, if you want to limit access to them.
  By default every address may connect, and the token and the login protect Furnace.

## 3. Install

You need one file:
- **Ubuntu and Debian:** `furnace_<version>_all.deb`, installed with `sudo apt install ./FILE`.
- **Red Hat family, Fedora, Amazon Linux:** `furnace-<version>-1.noarch.rpm`, installed with `sudo dnf install ./FILE`.

The examples below use version 0.8.0 and the `.deb`. With the `.rpm`, use `dnf` where they say `apt`.
Everything else (setup, questions, the `furnace` command) is the same on every system.

### 3.1 Get the package

- **Download it** from the GitHub releases page: <https://github.com/MahammadRahimov/furnace/releases>.
  The repository is private, so log in to GitHub first. Or, with the GitHub command line tool:
  ```bash
  gh release download v0.8.0 -R MahammadRahimov/furnace -p '*.deb'    # or -p '*.rpm'
  ```
- **Or build it yourself:** `packaging/build-deb.sh` on Ubuntu, Debian or WSL, or `packaging/build-rpm.sh` on a
  Red Hat family system (needs `rpm-build`). The file lands in `dist/`.
  The build needs `curl` and `python3`. The first build downloads the bundled libraries once (into
  `build/vendor-cache/`) and stops if one does not match its SHA-256 hash.
- Copy it to both servers, for example: `scp furnace_0.8.0_all.deb you@SERVER_IP:~/`

### 3.2 Leader

1. Install the package:
   ```bash
   sudo apt install ./furnace_0.8.0_all.deb
   ```
   apt also installs what Furnace needs from the system: nginx, keepalived, Python and a few tools.
   Nothing starts yet.
2. Set it up:
   ```bash
   sudo furnace setup
   ```
   Setup asks the questions from [section 4](#4-setup-questions-and-settings). Press Enter to take the answer in
   [brackets]. A wrong answer is explained and asked again. At the end you see all answers and confirm them.
   Example (fake addresses):
   ```
   Is this server the leader or the follower? [leader]:
   Network card name [eth0]:
   This server's IP [192.0.2.11]:
   The other Furnace server's IP: 192.0.2.12
   Shared IP: 192.0.2.10
   Network prefix of the shared IP (24 = 255.255.255.0) [24]:
   Who may call the API [any]:
   Who may download the lists [any]:
   Most entries per list [50000]:
   Days an IP stays blocked (permanent IPs never expire) [30]:
   VRRP router ID (1-255) [51]:
   SSH allowed from [any]:
   Let Furnace set up this server's own firewall (ufw)? [yes]:      (firewalld on the Red Hat family)

     The first API token, for the system that calls the API (for example a SOAR or a script).
   Token name [api]: soar

     What may this token do? Type a box number to switch it (or several: 2 4).

                      Read       Write
       Blocklist     1 [x]    2 [x]
       Whitelist     3 [x]    4 [x]

     Box number, or Enter when done: 4          (whitelist write off: this token may only read the whitelist)
     ...
     Box number, or Enter when done:

     Each token has its own expiry. Furnace warns 14 days before. 0 = never.
   Token expires after how many days: 365
   Set up Furnace with these settings? [yes]:
   ```
   The token questions come only on a leader that has no API token yet. The expiry has no default: type a number.
3. Setup configures and starts everything, then runs a self-test.
4. At the end it shows the **new secrets once**: the API token (with the name, permissions and expiry you chose)
   and the list login (user `lists` and a password). Store them now: the token in the caller (for
   example a SOAR credentials store), the login in the list reader (for example the firewall's EDL objects). They are not written to
   the log, and Furnace keeps only hashes, so it cannot show them again. Lost one? Make a new one
   ([section 7.1](#71-api-tokens-list-logins-and-the-lockout)).
5. It writes `/root/furnace-peer-bundle.tar.gz` for the follower. It holds the certificate, the
   replication key and the leader's settings. It is secret.

### 3.3 Follower

1. Copy the bundle from the leader to the follower:
   ```bash
   # on the leader
   sudo scp /root/furnace-peer-bundle.tar.gz you@FOLLOWER_IP:~/
   ```
2. On the follower, install the package and run setup:
   ```bash
   sudo apt install ./furnace_0.8.0_all.deb
   sudo furnace setup
   ```
3. Answer `follower`. Setup finds the bundle in your home folder, in `/root` or in `/tmp`, or asks where it is.
   It takes all shared settings from the leader, so it asks only for the network card. Then it deletes the bundle.
4. Delete the bundle copy wherever else you put it. Keep the one in `/root` on the leader only if you want to set
   up the follower again later.

### 3.4 Setup without questions

For automation, give the answers in a file:

```bash
cp /usr/share/doc/furnace/config.env.example config.env
nano config.env
sudo furnace setup --config config.env
```

For a follower, `ROLE=follower` is enough. Add `--bundle FILE` if the bundle is not in one of the usual places.

`sudo furnace setup --check` shows what setup would do and changes nothing.

### 3.5 What setup does

- Stops at the first error (strict mode) and tells you what went wrong.
- Checks that it runs as root on a supported system, and checks every answer.
- Saves the answers in `/etc/furnace/setup.conf` (readable by root only, it holds the keepalived password).
- Creates the user `furnace` (no login) and the user `furnace-sync` (can only receive replication snapshots).
- Leader: creates a self-signed certificate (10 years, names the shared IP and both server IPs), the replication
  SSH key and the keepalived password. Follower: copies them from the bundle. Both: the replication secret,
  derived from the certificate key they share (so both have the same one, without copying anything new).
- On the server that owns the shared IP: creates the first API token and the list login, if there are none yet.
  They are in the database, so the follower gets them with the replication.
- Writes the nginx and keepalived files from the templates in `/usr/lib/furnace/templates`.
- Opens only the needed ports on the host firewall (ufw or firewalld): HTTPS, SSH, and VRRP (protocol 112) between
  the servers. With firewalld it adds rich rules to the default zone and remembers them in
  `/etc/furnace/firewalld.rules`, so it can remove them again.
- With SELinux on (Red Hat family): labels the list folders for nginx (`httpd_sys_content_t`), the replication key
  folder for sshd (`ssh_home_t`) and the two keepalived scripts (`keepalived_unconfined_script_exec_t`), and turns on
  `httpd_can_network_connect` so nginx may pass API calls to Furnace on `127.0.0.1:8081`.
- Starts everything and runs a **self-test**: it adds the fake IP `192.0.2.123`, checks that nginx serves it in
  `blocklist.txt`, removes it, and checks that it is gone. It also checks that the archive and the database are
  not served, that the API refuses calls without a token, and that the lists refuse a missing or wrong login.
  The self-test uses its own short-lived token, so it never needs your tokens.
- Prints a summary of what worked and what you still must do by hand.

**Safe to run again.** Run `sudo furnace setup` again to change an answer. The brackets show the current answers.
It never wipes the database and never changes the tokens, the list logins or the certificate.
The log is in `/var/log/furnace-setup.log`. It never contains secrets.

## 4. Setup questions and settings

Each question saves one setting in `/etc/furnace/setup.conf`. The same names are used in a settings file for
`furnace setup --config`.

| Question | Setting | Default | Meaning |
|---|---|---|---|
| Leader or follower? | `ROLE` | `leader` | `leader` (preferred leader, keepalived priority 150) or `follower` (priority 100). |
| Network card name | `INTERFACE` | card of the default route | Name (not IP) of the card that carries this server's IP and the shared IP, for example `ens192`. See `ip -br addr`. |
| This server's IP | `THIS_IP` | first IP of the card | It must be set on that card. |
| The other server's IP | `PEER_IP` | | The other Furnace server. |
| Shared IP | `SHARED_IP` | | The shared IP (VIP, virtual IP). Callers and list readers always use this address. |
| Network prefix | `SHARED_IP_PREFIX` | prefix of this server's IP | For example 24 for 255.255.255.0. |
| Who may call the API | `API_ALLOW_FROM` | `any` | `any` = every address (a token is always needed). Or IPs and ranges, comma-separated, for example a SOAR server and its engines. |
| Who may download the lists | `LISTS_ALLOW_FROM` | `any` | `any` = every address (the list login is always needed). Or the list readers' IPs; for a firewall HA pair, list both. |
| Most entries per list | `MAX_IPS`, `MAX_DOMAINS` | `50000` | The list reader's limit per list. Adds beyond it are refused. 0 = no limit. The question sets both; a settings file can set them apart. |
| Days an IP stays blocked | `ARCHIVE_AFTER_DAYS` | `90` | Days a temporary IP stays in the list before the nightly job removes it. Permanent IPs and domains never expire. |
| VRRP router ID | `VRRP_ROUTER_ID` | `51` | 1-255. Same on both servers. Must be unique in the network segment. |
| SSH allowed from | `SSH_ALLOW_FROM` | `any` | Who may use SSH. `any` or a list of IPs. The other server is always allowed. |
| Set up the host firewall? | `HOST_FIREWALL` | `ufw` or `firewalld` | The tool on this server (`ufw` on the Debian family, `firewalld` on the Red Hat family) = configure it. `none` = leave it alone. |
| (not asked) | `KEEPALIVED_PASSWORD` | random | VRRP (Virtual Router Redundancy Protocol) password, 6-8 characters. Created on the leader. The follower gets it from the bundle. |
| (not asked) | `USE_HTTPS` | `yes` | `yes` = HTTPS. `no` = plain HTTP, for a test lab only (settings file only). |

Before 0.3.1, `API_ALLOW_FROM` was called `XSOAR_IP` and `LISTS_ALLOW_FROM` was called `FIREWALL_IP`. The old names
still work in a settings file. Setup saves the new names.

The follower takes `SHARED_IP`, `SHARED_IP_PREFIX`, `API_ALLOW_FROM`, `LISTS_ALLOW_FROM`, `MAX_IPS`, `MAX_DOMAINS`,
`ARCHIVE_AFTER_DAYS`, `KEEPALIVED_PASSWORD`, `VRRP_ROUTER_ID` and `USE_HTTPS` from the leader. If you change one of
them later, run `sudo furnace setup` on both servers with the same answer.

The program settings live in `/etc/furnace/furnace.env` (written by setup). They all start with `FURNACE_`:
`FURNACE_ARCHIVE_AFTER_DAYS`, `FURNACE_MAX_IPS`, `FURNACE_MAX_DOMAINS`, `FURNACE_BACKUP_KEEP` (14),
`FURNACE_CLEANUP_MAX_PERCENT` (20), `FURNACE_CLEANUP_MIN_SIZE` (100), `FURNACE_ALLOW_TEST_RANGES` (false).
During the setup self-test, test ranges are allowed for a few minutes by a file only root can create,
`/run/furnace-state/selftest`. This avoids restarting the API. A restart could make keepalived move the shared IP.
The file is removed after the test and is ignored when it is older than 10 minutes.
`FURNACE_LOCKOUT_FAILURES` (10) and `FURNACE_LOCKOUT_MINUTES` (15): an address that sends that many wrong tokens
within that time is locked for that time.
API tokens and list logins are not settings. They are in the database, as hashes only
([section 7.1](#71-api-tokens-list-logins-and-the-lockout)).

**The first API token.** Setup asks for it only on a leader that has no API token yet. The answers are used once
and are not saved in `setup.conf`. In a settings file for `furnace setup --config`:

| Question | Setting | Default | Meaning |
|---|---|---|---|
| Token name | `TOKEN_NAME` | `api` | 1 to 40 characters: letters, numbers, dot, dash or underscore. |
| The permission grid | `TOKEN_ALLOW` | `all` | Comma-separated: `blocklist:read`, `blocklist:write`, `whitelist:read`, `whitelist:write`, or `all`. Write includes read. |
| Token expires after how many days | `TOKEN_EXPIRES_DAYS` | asked without a default; `0` in a settings file | 0 to 3650. 0 = never. |

## 5. Checklist of manual work

Setup cannot do these things for you.

### 5.1 Network access

- [ ] List reader (its download source address) -> shared IP, TCP 443.
- [ ] Caller (and any helper servers it uses, for example SOAR engines) -> shared IP, TCP 443.
- [ ] Leader <-> follower: HTTPS (TCP 443) both ways, for replication and the health check.
- [ ] Leader <-> follower: SSH (TCP 22) both ways. Version 0.8 needs it only as the replication fallback while one
  server still runs an older Furnace (during an upgrade). The version after 0.8 no longer uses it.
- [ ] Leader <-> follower: VRRP (IP protocol 112) both ways, unicast.
- [ ] The network must allow the shared IP to move between the two servers (ARP updates). In a virtual
      environment (VMware, Hyper-V, cloud), check that MAC spoofing / IP moves are allowed for these VMs.
- [ ] Only if you limit `LISTS_ALLOW_FROM` or `API_ALLOW_FROM`: find out which source IP each device really uses.

### 5.2 The list reader (for example a firewall)

- [ ] Make it trust the Furnace certificate: `/etc/furnace/tls/furnace.crt` on the leader. It is public, not secret.
- [ ] Add two lists:
  - IPs: `https://SHARED_IP/edl/blocklist.txt`
  - Domains: `https://SHARED_IP/edl/domains.txt`
- [ ] Give it the list login: user `lists` and the password from setup (HTTP basic authentication).
- [ ] Let it check for updates every few minutes, and use the lists in its block rules.
- [ ] Check its limit per list. Set `MAX_IPS` to it (default 50,000). Furnace refuses new adds when a list is full
      (`limit_reached`).
- [ ] Furnace stores exact domains. Decide whether the reader should also match sub-domains.

If both Furnace servers are down, the list reader keeps using the last list it downloaded.
Palo Alto steps: [section 15.1](#151-palo-alto-firewall-list-reader).

### 5.3 The caller (for example a SOAR)

- [ ] Store its API token in its credentials store. Give every program its own token, with only the boxes it
      needs ([section 7.1](#71-api-tokens-list-logins-and-the-lockout)). Note the expiry date.
- [ ] Make it trust `furnace.crt` (see [6.10](#610-trusting-the-self-signed-certificate)).
- [ ] Build the HTTP calls with the API guide below. Always call the **shared IP**.
- [ ] Test with an IP from `203.0.113.0/24` first only if `FURNACE_ALLOW_TEST_RANGES=true`. In production, this is
      `false`, so test ranges are refused with `documentation_range`. That refusal is also a good first test.

Cortex XSOAR notes: [section 15.2](#152-cortex-xsoar-caller).

### 5.4 After both servers are installed

- [ ] Run the failover test on the leader: `sudo furnace failover-test`.
- [ ] Check the cleanup timer runs only on the server with the shared IP: `systemctl status furnace-cleanup.timer`.
- [ ] Keep `/root/furnace-peer-bundle.tar.gz` safe or delete it.

---

## 6. API guide

Furnace does not need a plugin, integration or pack. The caller sends plain HTTPS requests with JSON.
The full reference, with every endpoint, limit and answer, is in [docs/API.md](docs/API.md).

### 6.1 Basics

- Base address: `https://SHARED_IP` (always the shared IP, never a server's own IP).
- Format: JSON (JavaScript Object Notation) in and out. Send `Content-Type: application/json`.
- Authentication: the header `Authorization: Bearer <token>`. Every `/api/` call needs it.
  - Each token has its own permissions, a grid of four boxes:

    |  | Read | Write |
    |---|---|---|
    | **Blocklist** (`/api/v1/ips`, `/api/v1/domains`) | look up | block and unblock |
    | **Whitelist** (`/api/v1/whitelist/...`) | list | add, remove, bulk upload |

    Write includes read. A call without the needed box gets `401` with a message that names the permission.
  - Each token has its own expiry, chosen when it is made. An expired token gets `401`
    ("This token expired on ..."). It does not count towards the lockout.
  - Make tokens with `sudo furnace token add NAME` ([section 7.1](#71-api-tokens-list-logins-and-the-lockout)).
  - The full reference: [docs/API.md](docs/API.md).
- Wrong tokens: after 10 wrong tokens in 15 minutes, the calling address is locked for 15 minutes (`429`
  `locked`), even with a good token. A missing token does not count.
- By default every address may call the API. If `API_ALLOW_FROM` is limited, other addresses get `403` from nginx.
- Rate limit: 10 requests per second per caller (burst 50). Above that: `429`. Wait and try again.

The curl examples below use these shell variables:

```bash
FURNACE=https://192.0.2.10          # the shared IP
TOKEN='paste-a-token-with-blocklist-write-here'
WL_TOKEN='paste-a-token-with-whitelist-write-here'
CA=furnace.crt                       # the Furnace certificate file
```

### 6.2 Fields the caller should send

| Field | Required | Max length | What to send |
|---|---|---|---|
| `ip` or `domain` | yes | - | The indicator. One IP or one exact domain. |
| `added_by` | recommended | 100 | The analyst's name or user name (who clicked the button). |
| `reason` | recommended | 500 | Short reason, for example "Phishing C2 server". |

`incident_id` was removed in 0.8: a request that still sends it gets `400`. Put an incident number into `reason`
if you need it.

`removed_by` (optional) can be sent on DELETE as a query parameter: `?removed_by=analyst.name`.
All of this is stored with the entry and in the audit log.

### 6.3 Answers

Every answer is JSON with `status` and a plain English `message`. Show `message` to the analyst.

| HTTP code | `status` | Meaning |
|---|---|---|
| 201 | `added` | The entry is new and is now blocked (or whitelisted). |
| 200 | `already_exists` | It was already there. Nothing changed. The old date is kept. |
| 200 | `removed` | The entry was removed. |
| 200 | `found` | Look-up result. |
| 200 | `ok` | List or bulk result. |
| 400 | `bad_request` | The body is not valid JSON, or a field has the wrong type. |
| 401 | `unauthorized` | The token is missing or wrong, has expired, or lacks the permission for this call. `message` says which. |
| 403 | (nginx page) | `API_ALLOW_FROM` or `LISTS_ALLOW_FROM` is limited, and the caller's IP is not on it. |
| 404 | `not_found` | The entry is not in the list (or the address does not exist). |
| 405 | `method_not_allowed` | Wrong HTTP method. |
| 413 | `too_large` | The body is larger than 20 MB. |
| 422 | `rejected` | Refused. `reason_code` says why (table below). |
| 429 | (nginx page) | Too many requests. Slow down. |
| 429 | `locked` | Too many wrong tokens from this address. `Retry-After` says how many seconds to wait. |
| 503 | `rejected` + `not_leader` | You reached the standby server. Use the shared IP. |
| 503 | `busy` | Another change is running. Try again in a few seconds. |
| 500 | `error` | Unexpected server error. The details are only in the server log. |

Reason codes for `422` (`reason_code`):

| Code | Meaning |
|---|---|
| `missing_value` | No IP or domain was sent. |
| `invalid_ip` | Not a valid IP address. |
| `range_not_allowed` | A range (CIDR or a-b) was sent. Send one IP. |
| `private_ip`, `loopback_ip`, `link_local_ip`, `shared_address_space`, `multicast_ip`, `reserved_ip`, `unspecified_ip`, `not_public`, `ipv4_mapped_not_allowed` | Not a single public IP. |
| `documentation_range` | 192.0.2.0/24, 198.51.100.0/24 or 203.0.113.0/24, and test ranges are switched off. |
| `whitelisted` | The IP or domain is on the whitelist. The message names the whitelist entry. |
| `limit_reached` | The list is full (the firewall's EDL limit). |
| `invalid_domain` | Not a valid domain name. |
| `wildcard_not_allowed` | A wildcard like `*.example.com` was sent. |
| `url_not_allowed` | A URL was sent. Send only the domain. |
| `ip_not_allowed` | An IP was sent to the domain endpoint. |
| `invalid_cidr` | Whitelist: not a valid IP or range. |
| `range_too_wide` | Whitelist: shorter than /8 (IPv4) or /16 (IPv6). |
| `field_too_long` | `reason` or `added_by` is too long, or not text. |
| `invalid_permanent` | `permanent` is not `true` or `false`. |

How the caller should read an answer:

- `201` -> tell the analyst "blocked".
- `200` with `already_exists` -> tell the analyst "already blocked since <date>" (it is in `message` and `entry.added_at`).
- `422` -> show `message`. Do not retry. The input must change.
- `503`, `429` or a network error -> retry a few times with a pause. If it still fails, tell the analyst.
- `401` -> the token in the caller is wrong, expired or lacks a permission (see `message`). Alert its admin.
  Do not retry: wrong tokens lead to a lockout.
- `429` with `locked` -> fix the token, then ask the Furnace admin for `sudo furnace unlock <caller IP>`.

### 6.4 Add an IP

`POST /api/v1/ips` (blocklist write)

```bash
curl --cacert "$CA" -X POST "$FURNACE/api/v1/ips" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"ip":"203.0.113.45","reason":"Phishing C2 server","added_by":"analyst.one"}'
```

Optional: `"permanent": true` blocks the IP for ever. Without it (or with `false`) the IP is temporary: it leaves
the list after `ARCHIVE_AFTER_DAYS`.

Answer `201`:

```json
{"status": "added", "message": "The IP is now blocked for 90 days.",
 "entry": {"ip": "203.0.113.45", "added_at": "2026-10-26T09:15:02Z", "added_by": "analyst.one",
           "reason": "Phishing C2 server",
           "permanent": false, "expires_at": "2027-01-24T09:15:02Z"},
 "record": {"first_blocked_at": "2026-10-26T09:15:02Z", "last_blocked_at": "2026-10-26T09:15:02Z",
            "times_blocked": 1, "added_by": "analyst.one", "reason": "Phishing C2 server"}}
```

Answer `200` when it was already there:

```json
{"status": "already_exists",
 "message": "This IP is already blocked since 2026-10-20T08:00:00Z. Nothing changed.",
 "entry": {"ip": "203.0.113.45", "added_at": "2026-10-20T08:00:00Z", "...": "..."}}
```

Already in the list: nothing changes, also not `permanent`. The first block decides.

If the IP was blocked before (it is in the archive), it is blocked again with a new date: `201`,
`"restored_from_archive": true`, and the archive record counts one more block.

Answer `422`:

```json
{"status": "rejected", "message": "This is a private IP address. Only public IPs can be blocked.",
 "reason_code": "private_ip"}
```

### 6.5 Remove an IP

`DELETE /api/v1/ips/<ip>` (blocklist write)

```bash
curl --cacert "$CA" -X DELETE "$FURNACE/api/v1/ips/203.0.113.45?removed_by=analyst.one" \
  -H "Authorization: Bearer $TOKEN"
```

Answers: `200` `{"status": "removed", ...}` or `404` `{"status": "not_found", ...}`.

### 6.6 Look up an IP or a domain

`GET /api/v1/ips/<ip>` and `GET /api/v1/domains/<domain>` (blocklist read)

```bash
curl --cacert "$CA" "$FURNACE/api/v1/ips/203.0.113.45" -H "Authorization: Bearer $TOKEN"
```

```json
{"status": "found", "message": "This IP is blocked.", "list": "active",
 "entry": {"ip": "203.0.113.45", "added_at": "2026-10-26T09:15:02Z", "added_by": "analyst.one",
           "reason": "Phishing C2 server", "permanent": false,
           "expires_at": "2027-01-24T09:15:02Z"},
 "record": {"first_blocked_at": "2026-10-26T09:15:02Z", "...": "..."},
 "whitelisted_by": null}
```

`list` is `active` (blocked now) or `archive` (blocked before, not now). `404` means it was never blocked.
`record` is the archive record (first and last block, times blocked). Domains answer the same way.
`whitelisted_by` shows the whitelist entry that covers the IP, if any.

```bash
curl --cacert "$CA" "$FURNACE/api/v1/domains/bad.example.com" -H "Authorization: Bearer $TOKEN"
```

### 6.7 Add and remove a domain

`POST /api/v1/domains` and `DELETE /api/v1/domains/<domain>` (blocklist write)

```bash
curl --cacert "$CA" -X POST "$FURNACE/api/v1/domains" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"domain":"bad.example.com","reason":"Malware download site","added_by":"analyst.one"}'

curl --cacert "$CA" -X DELETE "$FURNACE/api/v1/domains/bad.example.com?removed_by=analyst.one" \
  -H "Authorization: Bearer $TOKEN"
```

Domains are stored in lower case. Domains never expire. They stay until someone removes them.
Domains have no `permanent` field. Like IPs, every domain gets an archive record.

### 6.8 Whitelist: add, remove, list

IPs and ranges: `/api/v1/whitelist/ips`. Domains: `/api/v1/whitelist/domains`. Listing needs whitelist read,
adding and removing need whitelist write.

```bash
# add an IP range (the field can be "entry", "cidr" or "ip")
curl --cacert "$CA" -X POST "$FURNACE/api/v1/whitelist/ips" \
  -H "Authorization: Bearer $WL_TOKEN" -H "Content-Type: application/json" \
  -d '{"entry":"198.51.100.0/24","reason":"Partner network","added_by":"admin.one"}'

# remove it (the slash in the range can stay as it is)
curl --cacert "$CA" -X DELETE "$FURNACE/api/v1/whitelist/ips/198.51.100.0/24" \
  -H "Authorization: Bearer $WL_TOKEN"

# list (paged: limit up to 10000, offset)
curl --cacert "$CA" "$FURNACE/api/v1/whitelist/ips?limit=1000&offset=0" -H "Authorization: Bearer $WL_TOKEN"

# domains
curl --cacert "$CA" -X POST "$FURNACE/api/v1/whitelist/domains" \
  -H "Authorization: Bearer $WL_TOKEN" -H "Content-Type: application/json" \
  -d '{"domain":"good.example.com","reason":"Company website","added_by":"admin.one"}'
curl --cacert "$CA" -X DELETE "$FURNACE/api/v1/whitelist/domains/good.example.com" \
  -H "Authorization: Bearer $WL_TOKEN"
```

When a whitelist entry is added, Furnace removes every blocked IP or domain it covers. The answer lists them in
`removed_from_blocklist`. Each removal is in the audit log.

### 6.9 Bulk upload (whitelist, and blocked IPs and domains)

`POST /api/v1/whitelist/ips/bulk` and `POST /api/v1/whitelist/domains/bulk` (whitelist write, also for `dry_run`).
Up to 200,000 entries per request. Use `dry_run` first to see what would happen.

```bash
# JSON
curl --cacert "$CA" -X POST "$FURNACE/api/v1/whitelist/ips/bulk" \
  -H "Authorization: Bearer $WL_TOKEN" -H "Content-Type: application/json" \
  -d '{"entries":["198.51.100.0/24","203.0.113.0/25","192.0.2.7"],"dry_run":true,
       "reason":"Partner ranges","added_by":"admin.one"}'

# or a plain text file, one entry per line (# comments allowed)
curl --cacert "$CA" -X POST "$FURNACE/api/v1/whitelist/ips/bulk?dry_run=true&added_by=admin.one" \
  -H "Authorization: Bearer $WL_TOKEN" -H "Content-Type: text/plain" --data-binary @whitelist.txt
```

Answer `200`:

```json
{"status": "ok", "message": "2 entries would be added. 1 entries would be rejected. 1 blocked IPs would be removed.",
 "dry_run": true, "received": 3, "added": 2, "already_exists": 0,
 "rejected": [{"value": "10.0.0.0/4", "reason_code": "range_too_wide", "message": "..."}],
 "removed_from_blocklist": ["203.0.113.45"]}
```

Bulk upload is all in one transaction. Bad lines are skipped and listed. Good lines are added.

**Blocked IPs and domains from a file:** `POST /api/v1/ips/bulk` and `POST /api/v1/domains/bulk` (blocklist write).
The same rules as `sudo furnace import-ips` and `import-domains` ([section 7.2](#72-import-ips)): dates, header and
comment lines, the whitelist, the archive, a backup first, all or nothing, and `MAX_IPS`. Options in the query
string: `dry_run`, `permanent`, `old_ips`, `date_format`, `reason`, `added_by`. Details: [docs/API.md](docs/API.md).

```bash
curl --cacert "$CA" -X POST "$FURNACE/api/v1/ips/bulk?dry_run=true" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: text/plain" --data-binary @blocklist.csv
```

### 6.10 Trusting the self-signed certificate

Do **not** switch off certificate checks ("insecure", "trust any certificate"). Trust the Furnace certificate
instead. It is the same certificate on both servers and names the shared IP.

1. Get the certificate: `/etc/furnace/tls/furnace.crt` on the leader. It is public, not secret.
2. Check its fingerprint on the server and on the copy you received:
   `openssl x509 -in furnace.crt -noout -fingerprint -sha256`
3. Give it to the program that makes the call:
   - curl: `--cacert furnace.crt`
   - Python requests: `requests.post(url, ..., verify="/path/to/furnace.crt")`
   - Linux trust store: copy to `/usr/local/share/ca-certificates/furnace.crt` and run `update-ca-certificates`.
   - A SOAR platform: add it as a trusted custom certificate for the server or engine that makes the call
     (Cortex XSOAR: [section 15.2](#152-cortex-xsoar-caller)).

The certificate is valid for 10 years.

### 6.11 Health check

`GET /health` needs no token. nginx allows it only from the servers themselves (localhost and the two server
IPs). Callers and list readers cannot reach it. It is for keepalived and the admins.

```json
{"status": "ok", "version": "0.8.0", "role": "leader", "ips": 1234, "domains": 56}
```

---

## 7. Daily admin work (the furnace command)

Run `furnace` with sudo on the server that owns the shared IP. `sudo furnace help` lists all commands.
The data commands run as the `furnace` user. A file to import that this user cannot read (for example in
`/root`) is copied to a temporary folder first, so any path works.

```bash
sudo furnace status                     # role, shared IP, services and counts; exit code 1 if something is wrong
```

```bash
sudo furnace stats                       # counts, dates, sizes, role
sudo furnace rebuild                     # rebuild blocklist.txt, domains.txt and archive.csv from the database
sudo furnace backup [--keep N]           # SQLite backup, keeps the newest N (default 14)
sudo furnace cleanup --dry-run           # which temporary IPs the nightly job would remove
sudo furnace cleanup [--force]           # run it now (--force skips the safety stop)
sudo furnace export ips -o /tmp/ips.csv  # also: archive (every IP and domain ever blocked), domains,
                                        # whitelist-ips, whitelist-domains, audit
sudo furnace check-config                # show the settings (no secrets)
```

### 7.1 API tokens, list logins and the lockout

API tokens and list logins are in the database. Furnace keeps only a hash (SHA-256 for tokens, SHA-512 crypt for
passwords). A new token or password is shown **once**. Lost one? Make a new one with `rotate`.
Changes run on the leader. The follower gets them with the next replication (within 20 seconds).
Every change is in the audit log, with the admin's name, never with the secret.

```bash
sudo furnace token list                       # permissions, expiry, last use and a short id, never the tokens
sudo furnace token add soar                   # asks the permission grid and the expiry, then shows the token
sudo furnace token add soar --allow blocklist:write,whitelist:read --expires-days 365   # the same, for scripts
sudo furnace token edit soar                  # change the boxes or the expiry; the token value stays
sudo furnace token rotate soar                # a new value; the old one stops working at once
sudo furnace token remove soar                # it stops working at once

sudo furnace edl-user list                    # the list logins (names only)
sudo furnace edl-user add firewall-2          # a new login with a random password
sudo furnace edl-user rotate lists            # a new password; give it to the list reader
sudo furnace edl-user remove firewall-2

sudo furnace lockouts                         # addresses locked after too many wrong tokens
sudo furnace unlock 198.51.100.20             # unlock one address (or: --all)
```

`token add` without options asks, like setup:

```
  What may this token do? Type a box number to switch it (or several: 2 4).

                   Read       Write
    Blocklist     1 [x]    2 [x]
    Whitelist     3 [x]    4 [ ]

  Box number, or Enter when done:

  Expires after how many days? (0 = never): 365
```

- **Permissions:** a number switches a box. Ticking Write also ticks Read. Switching Read off also switches Write
  off. At least one box must be ticked. `--allow` takes the same as a list: `blocklist:read`, `blocklist:write`,
  `whitelist:read`, `whitelist:write`, or `all`.
- **Expiry:** each token has its own, from 0 to 3650 days (0 = never). There is no default: you choose it for
  every token. `token rotate` starts the same number of days again (or give `--expires-days`). `token edit`
  counts new days from now.
- **Warnings:** a token that expires within 14 days, or has expired, is shown with `WARNING` in
  `sudo furnace status` and in the nightly cleanup log (`journalctl -u furnace-cleanup`). `token list` shows
  `EXPIRED` for it. Rotate it before the date and give the new value to the program.
- **Last use:** `token list` shows when each token was last used (at most once an hour, on the leader).
- Without a terminal (a script, `ssh host sudo furnace ...`), `token add` needs both `--allow` and `--expires-days`.

Give every program its own token (for example `soar` and, later, `ui`), with only the boxes it needs. Then you can
replace or delete one without touching the others. The audit log shows which token made each change
(`source` = `api:NAME@IP`).

The lockout is kept on each server and is not replicated. `furnace unlock` works on the server where you run it,
so run it on the server that owns the shared IP.

A database backup also runs every day at 01:30 (`furnace-backup.timer`).

### 7.2 Import IPs

```bash
sudo furnace import-ips /tmp/old-list.csv --dry-run     # check first, change nothing
sudo furnace import-ips /tmp/old-list.csv
```

- Line formats: `ip`, `ip,date` or `ip,date,reason`.
- Separators: comma, semicolon, tab or spaces.
- Header lines, blank lines and comment lines (`#`, `;`, `//`) are skipped.
- Dates: `2026-05-20`, `2026-05-20 08:30:00`, ISO 8601 (`2026-05-20T08:30:00Z`), `2026/05/20`, `20.05.2026`,
  `20 May 2026`, Unix time in seconds or milliseconds, and `dd/mm/yyyy` or `mm/dd/yyyy`.
  - A date like `05/06/2026` is never guessed. If another line in the file shows the order (for example
    `25/05/2026`), Furnace uses that order. Otherwise the line is skipped as `ambiguous_date`.
  - Or give the format: `--date-format "%d/%m/%Y"`.
- Real dates from the file are kept.
- IPs with a date older than `ARCHIVE_AFTER_DAYS` go only into the archive (they were blocked, not now).
  With `--old-ips fresh` they go into the active list with a new date instead. Their expiry is spread out, like
  undated IPs, so they do not all expire on the same night.
- IPs with no date get their expiry spread evenly over the next `ARCHIVE_AFTER_DAYS` days.
- `--permanent` blocks every IP of the file permanently. Dates are kept, nothing is too old.
- Every added IP gets an archive record. An IP already in the archive counts one more block.
- Skipped lines (invalid, private, whitelisted, duplicate, already active, bad date) are written with their reason
  to a report file in `/var/lib/furnace/reports/` (or `--report FILE`).
- Every import takes a database backup first and is all or nothing.
- If the result would be larger than `MAX_IPS`, the import stops and changes nothing.
- 30,000 lines take about 2 seconds.

### 7.3 Import domains and whitelists

```bash
sudo furnace import-domains /tmp/domains.txt [--dry-run]            # domain or domain,reason
sudo furnace import-whitelist-ips /tmp/wl-ips.txt [--dry-run]       # IPs and CIDR ranges
sudo furnace import-whitelist-domains /tmp/wl-domains.txt [--dry-run]
```

## 8. Rules for IPs, domains and the whitelist

**IPs**
- Single public IPs only (IPv4 and IPv6).
- Refused: ranges, private, loopback, link-local, shared (100.64.0.0/10), multicast, reserved.
- The documentation ranges 192.0.2.0/24, 198.51.100.0/24 and 203.0.113.0/24 are refused unless
  `FURNACE_ALLOW_TEST_RANGES=true` (tests and the setup self-test only).
- Refused when on the whitelist.
- Temporary (default): leaves the list after `ARCHIVE_AFTER_DAYS`. Permanent (`"permanent": true`): never expires,
  only an unblock removes it.
- Already active: nothing changes (also not temporary or permanent), the old date is kept, and the answer says so.
- In the archive and added again: back in the list with a new date.
- Stored for each IP: date added, who added it, reason, temporary or permanent.
- `furnace status` warns when permanent IPs fill half of `MAX_IPS`, because they never leave the list.

**Domains**
- Exact domains only. Refused: wildcards, URLs, IPs, invalid names. Stored in lower case.
  Non-ASCII names are stored as punycode (`xn--...`).
- No expiry: a domain stays until someone removes it.
- Refused when on the domain whitelist. The domain whitelist is exact: whitelisting `example.com` does not
  whitelist `www.example.com`.

**Whitelist**
- IPs and CIDR (Classless Inter-Domain Routing) ranges, and exact domains.
- Ranges shorter than /8 (IPv4) or /16 (IPv6) are refused.
- Adding an entry removes the blocked IPs or domains it covers, with an audit entry.
- Look-ups use merged, sorted ranges and binary search. Checking 30,000 IPs against thousands of ranges takes
  milliseconds.

## 9. The nightly cleanup and the archive

Furnace keeps two lists:

- **The EDL lists** (`blocklist.txt`, `domains.txt`): what is blocked now.
- **The archive**: a record of every different IP and domain that was ever blocked. One row each, never a
  duplicate, never removed. It is written when an IP or domain is blocked for the first time. The row keeps the
  first details (date, who, reason), the last block date and how many times it was blocked.
  Expiry, unblock and the whitelist remove an entry only from the EDL list, never from the archive.

The nightly cleanup:

- `furnace-cleanup.timer` runs every night at 02:30, only on the server that owns the shared IP.
- It removes temporary IPs older than `ARCHIVE_AFTER_DAYS` from the list in one transaction and rebuilds
  `blocklist.txt`. Permanent IPs and domains are never removed.
- It rewrites `/var/lib/furnace/archive/archive.csv` (IPs and domains, with `blocked_now` yes or no). Imports and
  `furnace rebuild` write it too. `sudo furnace export archive` always gives the current state.
- The archive CSV is only storage. It is not an EDL. nginx never serves it.
- **Safety stop:** if one run would remove more than 20 percent of the list, and the list has at least 100 IPs,
  the job changes nothing, writes an audit entry and exits with code 3. systemd marks the run as failed.
  Check the dates. If the removal is correct, run `sudo furnace cleanup --force`.
- `sudo furnace cleanup --dry-run` shows which IPs would be removed.
- Logs: `journalctl -u furnace-cleanup`.

## 10. Two servers: leader and follower

- keepalived holds the **shared IP** on the current leader. It uses unicast VRRP between the two servers.
- Both servers use state `BACKUP` with `nopreempt`. A server that comes back does **not** take the IP back.
  Priority 150 is the preferred leader at the very first start, 100 is the follower.
- keepalived calls `/health` every 5 seconds. After 3 failures the IP moves, even if the machine is still alive.
  `/health` fails when nginx or the API is stopped or hangs, when the database cannot be read, when an EDL file
  is missing, and when the disk is full. Tested in a lab: the follower takes over in about 15 seconds and the
  firewall keeps getting the list.
- The health check does **not** fail on an expired certificate. Both servers share one certificate, so it would
  fail on both at the same moment and nobody would hold the shared IP. Watch the daily certificate warnings instead.
- If keepalived itself crashes, systemd starts it again within 2 seconds
  (`/etc/systemd/system/keepalived.service.d/furnace.conf`). At start it drops the shared IP and joins as the
  standby, so the IP is never left on both servers.
- When a server takes the shared IP, the notify script marks it as leader and starts the cleanup timer.
  When it loses the IP, it marks it as follower and stops the timer.
- **Replication:** every 20 seconds the leader makes a SQLite backup snapshot and sends it over HTTPS to
  `https://PEER_IP/replica/snapshot`, with the replication secret both servers share. nginx lets only the other
  server reach this address. The follower checks the secret, loads the snapshot, rebuilds its EDL files and
  answers at once, so the leader knows that it worked. `sudo furnace status` on the leader shows the last result.
- **SSH fallback (this version only):** if HTTPS does not work, for example while the follower still runs an older
  Furnace during an upgrade, the leader pushes the snapshot over SSH as before (a dedicated key that can only run
  the receive command). `furnace status` then says so. The version after 0.8 removes SSH replication.
- The follower refuses all changes while it is not the leader (`503 not_leader`).

### 10.1 Risks

- **Split brain.** If the two servers cannot see each other but both are alive, both take the shared IP.
  Each one accepts changes, and neither takes snapshots from the other. When the link comes back, one keeps the IP.
  Changes made on the other server during the split are overwritten by the next snapshot. Check the audit log of
  both servers after a network problem. Keep VRRP and SSH between the servers on a reliable path.
- **Up to 20 seconds of lost changes.** If the leader dies, changes from its last 20 seconds may not have reached the
  follower. The caller got a success answer for them. Ask analysts to re-click after a failover, or check the audit log.
  The duplicate rule makes a re-click harmless.
- **Both servers down.** The firewall keeps the last list it downloaded and keeps blocking. New blocks are not
  possible until a server is back.
- Replication sends the whole database every 20 seconds. For large audit logs, this is more network traffic.

### 10.2 Failover test

Run it on the server that owns the shared IP:

```bash
sudo furnace failover-test        # stops keepalived here (like a dead machine)
sudo furnace failover-test --web  # stops nginx here (the web service breaks)
```

It stops the service on purpose, waits for the other server to take the shared IP, checks that it answers on the
shared IP as the leader, and then starts the service again. Afterwards the other server stays the leader.

## 11. Files and folders

| Path | What |
|---|---|
| `/usr/bin/furnace` | The command. |
| `/usr/lib/furnace/` | The program, setup, templates and helper scripts (keepalived, replication, failover test). |
| `/usr/lib/furnace/vendor/` | The bundled libraries: Flask, gunicorn and what they need. |
| `/usr/lib/furnace/bin/furnace-python` | Starts Python 3.9+ with Furnace and the bundled libraries. Every Furnace service uses it. |
| `/lib/systemd/system/furnace-*` | The systemd units. |
| `/etc/furnace/setup.conf` | Your setup answers. Readable by root only. |
| `/etc/furnace/furnace.env` | Program settings (no secrets). |
| `/etc/furnace/tls/` | Certificate and private key. |
| `/etc/furnace/secrets/replica-secret` | The replication secret (from the certificate key). Root and the furnace group. |
| `/etc/furnace/ssh/` | SSH replication key (the fallback in this version). |
| `/var/lib/furnace/furnace.db` | The database (master record), with the API tokens and list logins as hashes. |
| `/var/lib/furnace/lockout.db` | Addresses locked after wrong tokens (this server only). |
| `/var/lib/furnace-auth/edl-users` | List logins for nginx (name and hash), written from the database. Never served. |
| `/var/lib/furnace/archive/archive.csv` | The archive record of IPs and domains as CSV. Never served. |
| `/var/lib/furnace/backups/` | Database backups. |
| `/var/lib/furnace/reports/` | Import reports. |
| `/var/lib/furnace-edl/` | `blocklist.txt` and `domains.txt` (served by nginx). |
| `/var/lib/furnace-incoming/` | Snapshots received from the other server. |
| `/run/furnace-state/role` | `leader` or `follower` (written by keepalived). |
| `/var/log/furnace-setup.log` | Setup log. No secrets. |
| `/root/furnace-new-secrets.txt` | Only when setup made new secrets without a person at the terminal (package upgrade). Read it, then delete it. |

Services: `furnace-api` (gunicorn), `nginx`, `keepalived`. Timers: `furnace-cleanup`, `furnace-backup`,
`furnace-replicate`. Path unit: `furnace-apply-replica`.
Logs: `journalctl -u furnace-api`, `/var/log/nginx/furnace-access.log`.

## 12. Upgrade and uninstall

**Upgrade:** install the new package. Do the follower first, then the leader.

```bash
sudo apt install ./furnace_0.8.0_all.deb
```

The package runs `furnace setup --upgrade` by itself. It uses the saved answers and asks no questions.
Data, tokens and the certificate are kept. The API is reloaded without downtime, and keepalived is restarted only
if its settings changed, so an upgrade does not move the shared IP. If you change answers that affect keepalived
(for example the VRRP router ID), the restart can move the shared IP to the other server. That is safe.

**From 0.7 to 0.8:** nothing to do. Replication now goes over HTTPS. Setup makes the replication secret from
the certificate key both servers already share, so no new bundle is needed. While one server is upgraded and the
other is not yet, the leader uses SSH as before; when both run 0.8 it uses HTTPS. Port 443 between the servers is
already open (the health check uses it). New: blocklist files through the API (section 6.9).
**Change your callers first:** the field `incident_id` is gone. A request that still sends it gets `400`, so remove
it from your SOAR playbooks and scripts before the upgrade. The upgrade deletes every stored incident number (old
backups still have them). The exports and the archive CSV no longer have the `incident_id` column.

**From 0.6 to 0.7:** nothing to do. The database gets the archive record: every IP and domain in the lists, and
every IP of the old archive, gets a record row. IPs in the list stay temporary with their dates. Domains stay as they
are. Upgrade both servers the same day: a follower that still runs 0.6 and becomes the leader would let permanent
IPs expire.

**From 0.4 to 0.5:** nothing to do.

**From 0.3 to 0.4:** nothing to do. Flask and gunicorn now come with Furnace. The system's
`python3-flask` and `python3-gunicorn` are no longer needed; remove them with `sudo apt autoremove` if nothing else
uses them.

**From 0.5 to 0.6:** nothing to do. The tokens keep their rights and never expire: `block` gets blocklist
read+write, `whitelist` gets whitelist read+write, a token with the scope `all` gets all four boxes. Upgrade the
leader first. Until the follower is upgraded too, it refuses tokens with other box combinations (it does not know
them); after a failover in that time, upgrade the follower.

**From 0.3.0 to 0.3.1:** nothing to do. The settings get their new names (`API_ALLOW_FROM`,
`LISTS_ALLOW_FROM`). Tokens and logins made by 0.3.0 keep their names (`xsoar`, `xsoar-whitelist`, `paloalto`).

**From 0.2 to 0.3:** do the **leader first**, then the follower.
- The API tokens of 0.2 keep working. They move from `/etc/furnace/tokens.env` into the database (names `block`
  and `whitelist`), and the file is deleted.
- The lists now need a login. The upgrade makes the login `lists` and writes its password to
  `/root/furnace-new-secrets.txt`. Until you give it to the list reader, the list reader cannot download new
  lists and keeps the last one it has. Delete the file afterwards.
- Your saved IP limits stay. To open them to every address, run `sudo furnace setup` on both
  servers and answer `any`.

If the upgrade step reports a problem, the package stays installed. Fix the problem and run
`sudo furnace setup --upgrade`. The details are in `/var/log/furnace-setup.log`.

**Uninstall:**

```bash
# Debian family
sudo apt remove furnace    # removes the program and stops the services, keeps the data, tokens and logins
sudo apt purge furnace     # also deletes the database, backups, tokens, logins, certificate and users

# Red Hat family
sudo dnf remove furnace    # removes the program and stops the services, keeps the data, tokens and logins
```

Removing the package also removes the Furnace nginx site, the keepalived configuration (the shared IP is released),
the Furnace firewall rules (ufw or firewalld; the firewall itself stays on) and the Furnace SELinux labels.
Installing the package again later uses the kept data.

`dnf remove furnace` also removes the packages that only Furnace needed and dnf installed for it (for example nginx,
keepalived, or a `python3.12` on RHEL 8). If the SSH server (`openssh-server`) came in only with Furnace, it goes
too: check the list dnf shows before you answer yes, or keep it with `sudo dnf mark install openssh-server` first.

RPM has no "purge". To delete all Furnace data after `dnf remove furnace`:

```bash
sudo rm -rf /var/lib/furnace /var/lib/furnace-edl /var/lib/furnace-auth /var/lib/furnace-incoming \
    /var/lib/furnace-sync /etc/furnace /root/furnace-peer-bundle.tar.gz /var/log/furnace-setup.log
sudo userdel furnace-sync; sudo userdel furnace
```

SELinux: setup turns on `httpd_can_network_connect`. Removing Furnace leaves it on, because other programs may need
it. Turn it off with `sudo setsebool -P httpd_can_network_connect off` if nothing else uses it.

## 13. Tests

Unit tests (run on Linux, because the file lock uses `fcntl`):

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

They cover validation, the duplicate rule, permanent IPs, the archive record of IPs and domains, the upgrade
from 0.6, restore from the archive, the cleanup with fake dates and the
`ARCHIVE_AFTER_DAYS` setting, the safety stop, the whitelist, imports (dated and undated, dry-run, formats), the
API (token permissions and expiry, rotate, edit and remove, last use, the lockout, the setup self-test token,
errors), list logins (checked with openssl), the token migration from 0.2 and from 0.5, a snapshot from an older
leader, the token commands and the permission grid, many requests at the same time (threads and processes), and that
no temporary files are left behind.

Integration test with Docker (two servers, a fake list reader and a fake caller; only test addresses).
It builds the `.deb`, installs it with apt and answers the setup questions through a terminal:

```bash
tests/integration/run.sh ubuntu:24.04
tests/integration/run.sh ubuntu:22.04
tests/integration/run.sh debian:12
tests/integration/run.sh debian:13
tests/integration/run.sh almalinux:9        # also: almalinux:8, almalinux:10, rockylinux:9, oraclelinux:9,
                                            #       fedora:42, amazonlinux:2023
```

All tests use only 192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24, the IPv6 documentation range 2001:db8::/32
(for volume tests only) and names under `example.com`, `example.net` and `example.org`.

Results for v0.7.0 (every system with the same final package):

| System | Checks |
|---|---|
| Ubuntu 22.04, Ubuntu 24.04, Debian 12, Debian 13 (`.deb`) | 116 of 116 each (one more: `apt purge`) |
| AlmaLinux 8, 9, 10, Rocky Linux 9, Oracle Linux 9, Fedora 42, Amazon Linux 2023 (`.rpm`) | 115 of 115 each |

It covers:
building the package, `apt install` with its dependencies, Flask and gunicorn from the bundle (not the system), nothing running before setup, `setup --check`, refusing
bad settings, a follower without a bundle, the leader setup by answering the questions (including a wrong answer
that is asked again, the card and IP found automatically, the keepalived password created), the follower setup with
only two questions, certificate and settings copied through the bundle, the secrets shown once and never in the
log or the database, the self-test, API and EDL access through the shared IP, the list login (no login or a wrong
password is refused), the first token from the setup questions (permission grid and expiry), token permissions
(read without write), an expired token and the status warning, the token commands, the wrong-token lockout and
unlock, tokens and logins
replicated to the follower, nginx and ufw rules with limited IPs and with `any` (archive and /health never served),
replication both ways, the follower refusing changes, `furnace status`, an import from a file only root can read,
a package upgrade that keeps data and tokens without moving the shared IP, both failover modes, the cleanup timer
moving with the shared IP, `apt remove` (data kept) and `apt purge` (data deleted).

On the Red Hat family the same test installs the `.rpm` with dnf, uses firewalld, and checks that `dnf remove`
keeps the data and removes the firewall rules.

The unit tests also pass with the bundled libraries on Python 3.9 (the oldest supported) and Python 3.13
(Debian 13).

**SELinux test** on a real AlmaLinux 9.8 VM with SELinux enforcing (26 of 26 checks, v0.5.0): install with dnf,
setup without questions (secrets written to the root-only file), the SELinux labels, keepalived making the server
leader and running its scripts, nginx serving the lists with the login and passing API calls to Furnace through the
shared IP, the health check, the replication login through sshd, the replication timer running ssh, the firewalld
rules, and **no SELinux denials** (counted in the raw audit log after the timers had run). `dnf remove` then left no
Furnace firewall rules or SELinux labels behind. This test found one problem that containers cannot show (the
replication timer was not allowed to run ssh); it is fixed.

## 14. What was not tested

Be aware of these limits before you go live:

- **A real Palo Alto firewall.** The EDL download, also with the EDL login, was tested with curl from a container.
  The PAN-OS menus in this README come from documentation and may differ in your version.
- **A real XSOAR server.** The API calls were tested with curl. XSOAR's own way to trust a certificate was not
  tested.
- **Debian, the Red Hat family, Fedora and Amazon Linux on real servers.** They were tested in Docker containers,
  where SELinux is off. SELinux was tested on one AlmaLinux VM only (see section 13).
- **keepalived on real servers or VMs.** It was tested only in Docker containers on one host. Real switches,
  VMware/Hyper-V port security, ARP behaviour and a real network outage (split brain) were not tested.
- **A real power cut.** SQLite `synchronous=FULL` and atomic renames are designed for this, but it was not tested.
- **Long running time.** No test ran for days. Audit log growth and backup rotation over months were not tested.
- **Large real lists with the firewall.** 30,000-line imports were timed (about 2 seconds), but not loaded by a
  firewall.
- **A server without its system's package mirror.** apt needs the mirror, a proxy or a local mirror for the
  dependencies. An install without any of them was not tested.
- **`furnace setup` over a real SSH session.** The questions were answered through a terminal in a container,
  not by a person over SSH.
- **An upgrade from 0.2 on real servers.** The token migration was tested in unit tests, not with a real 0.2
  install.
- **`USE_HTTPS=no`** (HTTP test mode; the EDL password would travel in clear text), `HOST_FIREWALL=none` and `furnace setup --config` for a follower were
  not run in the integration test.

## 15. Examples: Palo Alto and Cortex XSOAR

These are the tested products. Menu names can differ between versions. Other products need the same things:
trust the certificate, the two list URLs with the list login, and the API token.

### 15.1 Palo Alto firewall (list reader)

Palo Alto calls the lists EDLs (External Dynamic Lists).

**Certificate**

1. Copy `/etc/furnace/tls/furnace.crt` from the leader.
2. **Device > Certificate Management > Certificates > Import**. Import `furnace.crt`. Mark it as
   **Trusted Root CA**.
3. **Device > Certificate Management > Certificate Profile > Add**. Add `furnace.crt` as the CA (Certificate
   Authority).
4. Commit.

**EDL objects**

1. **Objects > External Dynamic Lists > Add**:
   - Name: for example `Furnace-IP-Blocklist`
   - Type: **IP List**
   - Source: `https://SHARED_IP/edl/blocklist.txt`
   - Certificate Profile: the profile from above
   - Client Authentication: the list login (user `lists` and its password)
   - Check for updates: **Every five minutes**
2. Add a second EDL:
   - Type: **Domain List**
   - Source: `https://SHARED_IP/edl/domains.txt`
   - Same certificate profile, list login and update interval.
3. Use the IP list in security policy rules (deny, source and/or destination).
   Use the domain list in an Anti-Spyware profile (DNS policies) or as your design requires.
4. Commit. Check that the firewall downloaded the lists (in the EDL object, "Test Source URL", or on the CLI:
   `request system external-list show type ip name Furnace-IP-Blocklist`).
5. Check your firewall model's EDL limit and set `MAX_IPS` to it.

The firewall downloads EDLs from its management interface by default. With a **service route** for External
Dynamic Lists, it uses that interface. That matters only if you limit `LISTS_ALLOW_FROM`.

### 15.2 Cortex XSOAR (caller)

- Store the API token in the XSOAR credentials store (a token with blocklist write; add whitelist write only if
  XSOAR changes the whitelist).
- Furnace does not need an XSOAR integration or pack. Use plain HTTP calls (for example the generic HTTP
  integration or a script) as described in [section 6](#6-api-guide).
- Send the analyst's name in `added_by`. If you want the XSOAR incident number in Furnace, put it into
  `reason` (for example `Phishing C2 (incident 4521)`).
- Trust the certificate: add `furnace.crt` as a trusted custom certificate for the server or engine that makes the
  call. Look for "custom certificates" or "trusted certificates" in the XSOAR documentation for your version.
- If XSOAR uses engines, the calls come from the engines' IPs. That matters only if you limit `API_ALLOW_FROM`.
