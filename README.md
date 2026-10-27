# Furnace

Furnace is a standalone blocklist server for a Palo Alto firewall.

SOC (Security Operations Center) analysts click a button in Cortex XSOAR (Security Orchestration, Automation and Response).
XSOAR calls the Furnace API (Application Programming Interface) with an IP address or a domain.
Furnace saves it and publishes it as an EDL (External Dynamic List).
An EDL is a plain text file. The firewall downloads it again and again and blocks what is in it.

Furnace keeps working when XSOAR is down. Only the button stops working.
The firewall keeps downloading the lists from Furnace.

## Contents

1. [How it works](#1-how-it-works)
2. [What you need](#2-what-you-need)
3. [Install](#3-install)
4. [Setup questions and settings](#4-setup-questions-and-settings)
5. [Checklist of manual work](#5-checklist-of-manual-work)
6. [API guide for the XSOAR engineer](#6-api-guide-for-the-xsoar-engineer)
7. [Daily admin work (the furnace command)](#7-daily-admin-work-the-furnace-command)
8. [Rules for IPs, domains and the whitelist](#8-rules-for-ips-domains-and-the-whitelist)
9. [The nightly cleanup and the archive](#9-the-nightly-cleanup-and-the-archive)
10. [Two servers: leader and follower](#10-two-servers-leader-and-follower)
11. [Files and folders](#11-files-and-folders)
12. [Upgrade and uninstall](#12-upgrade-and-uninstall)
13. [Tests](#13-tests)
14. [What was not tested](#14-what-was-not-tested)

---

## 1. How it works

```
 Analyst --button--> XSOAR --HTTPS API call--> Furnace (shared IP) --> SQLite database
                                                       |
                                                       +--> blocklist.txt  (IPs)
                                                       +--> domains.txt    (domains)
                                                                 ^
 Palo Alto firewall --downloads every few minutes over HTTPS-----+
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

Technology: Ubuntu 22.04 or 24.04, Python 3 with Flask, gunicorn, SQLite in WAL mode (Write-Ahead Logging) with
`synchronous=FULL`, nginx, systemd and keepalived.

## 2. What you need

- Two Ubuntu servers (22.04 or 24.04) in the same network segment. One is enough for a lab.
- Three IP addresses in that segment: one per server and one **shared IP** (VIP, virtual IP).
- The servers need the Ubuntu package mirror (directly, through a proxy, or a local mirror). apt installs
  nginx, keepalived, Flask and gunicorn from there. PyPI (the Python Package Index) is not needed.
- Optional: the IP address the firewall uses to download EDLs, and the IP address of XSOAR, if you want to limit
  access to them. By default every address may connect, and the token and the login protect Furnace.

## 3. Install

You need one file: `furnace_<version>_all.deb`. The same file works on Ubuntu 22.04 and 24.04.
The examples below use version 0.3.0.

### 3.1 Get the package

- **Download it** from the GitHub releases page: <https://github.com/MahammadRahimov/furnace/releases>.
  The repository is private, so log in to GitHub first. Or, with the GitHub command line tool:
  ```bash
  gh release download v0.3.0 -R MahammadRahimov/furnace -p '*.deb'
  ```
- **Or build it yourself** on Ubuntu or WSL: `packaging/build-deb.sh`. The file lands in `dist/`.
- Copy it to both servers, for example: `scp furnace_0.3.0_all.deb you@SERVER_IP:~/`

### 3.2 Leader

1. Install the package:
   ```bash
   sudo apt install ./furnace_0.3.0_all.deb
   ```
   apt also installs what Furnace needs from Ubuntu: nginx, keepalived, Flask, gunicorn and a few tools.
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
   Who may call the API (XSOAR) [any]:
   Who may download the lists (the firewall) [any]:
   Most entries per list [50000]:
   Days an IP stays blocked before it moves to the archive [30]:
   VRRP router ID (1-255) [51]:
   SSH allowed from [any]:
   Let Furnace set up this server's own firewall (ufw)? [yes]:
   Set up Furnace with these settings? [yes]:
   ```
3. Setup configures and starts everything, then runs a self-test.
4. At the end it shows the **new secrets once**: two API tokens (`xsoar` for blocking, `xsoar-whitelist` for
   the whitelist) and the EDL login for the firewall (user `paloalto` and a password). Store them now:
   the tokens in the XSOAR credentials store, the login in the firewall's EDL objects. They are not written to
   the log, and Furnace keeps only hashes, so it cannot show them again. Lost one? Make a new one
   ([section 7.1](#71-api-tokens-edl-logins-and-the-lockout)).
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
   sudo apt install ./furnace_0.3.0_all.deb
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
- Checks that it runs as root on Ubuntu 22.04 or 24.04, and checks every answer.
- Saves the answers in `/etc/furnace/setup.conf` (readable by root only, it holds the keepalived password).
- Creates the user `furnace` (no login) and the user `furnace-sync` (can only receive replication snapshots).
- Leader: creates a self-signed certificate (10 years, names the shared IP and both server IPs), the replication
  SSH key and the keepalived password. Follower: copies them from the bundle.
- On the server that owns the shared IP: creates the first API tokens and the EDL login, if there are none yet.
  They are in the database, so the follower gets them with the replication.
- Writes the nginx and keepalived files from the templates in `/usr/lib/furnace/templates`.
- Opens only the needed ports on the host firewall (ufw): HTTPS, SSH, and VRRP (protocol 112) between the servers.
- Starts everything and runs a **self-test**: it adds the fake IP `192.0.2.123`, checks that nginx serves it in
  `blocklist.txt`, removes it, and checks that it is gone. It also checks that the archive and the database are
  not served, that the API refuses calls without a token, and that the lists refuse a missing or wrong login.
  The self-test uses its own short-lived token, so it never needs your tokens.
- Prints a summary of what worked and what you still must do by hand.

**Safe to run again.** Run `sudo furnace setup` again to change an answer. The brackets show the current answers.
It never wipes the database and never changes the tokens, the EDL logins or the certificate.
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
| Shared IP | `SHARED_IP` | | The shared IP (VIP, virtual IP). The firewall and XSOAR always use this address. |
| Network prefix | `SHARED_IP_PREFIX` | prefix of this server's IP | For example 24 for 255.255.255.0. |
| Who may call the API | `XSOAR_IP` | `any` | `any` = every address (a token is always needed). Or IPs and ranges, comma-separated, for example XSOAR and its engines. |
| Who may download the lists | `FIREWALL_IP` | `any` | `any` = every address (the EDL login is always needed). Or the firewall's IPs; for a firewall HA pair, list both. |
| Most entries per list | `MAX_IPS`, `MAX_DOMAINS` | `50000` | The firewall's EDL limit. Adds beyond it are refused. 0 = no limit. The question sets both; a settings file can set them apart. |
| Days before the archive | `ARCHIVE_AFTER_DAYS` | `30` | Days an IP stays active before the nightly job archives it. |
| VRRP router ID | `VRRP_ROUTER_ID` | `51` | 1-255. Same on both servers. Must be unique in the network segment. |
| SSH allowed from | `SSH_ALLOW_FROM` | `any` | Who may use SSH. `any` or a list of IPs. The other server is always allowed. |
| Set up ufw? | `HOST_FIREWALL` | `ufw` | `ufw` = configure the host firewall. `none` = leave it alone. |
| (not asked) | `KEEPALIVED_PASSWORD` | random | VRRP (Virtual Router Redundancy Protocol) password, 6-8 characters. Created on the leader. The follower gets it from the bundle. |
| (not asked) | `USE_HTTPS` | `yes` | `yes` = HTTPS. `no` = plain HTTP, for a test lab only (settings file only). |

The follower takes `SHARED_IP`, `SHARED_IP_PREFIX`, `XSOAR_IP`, `FIREWALL_IP`, `MAX_IPS`, `MAX_DOMAINS`,
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
API tokens and EDL logins are not settings. They are in the database, as hashes only
([section 7.1](#71-api-tokens-edl-logins-and-the-lockout)).

## 5. Checklist of manual work

Setup cannot do these things for you.

### 5.1 Network access

- [ ] Firewall (EDL download source address) -> shared IP, TCP 443.
- [ ] XSOAR (and any XSOAR engines) -> shared IP, TCP 443.
- [ ] Leader <-> follower: SSH (TCP 22) both ways, for replication.
- [ ] Leader <-> follower: VRRP (IP protocol 112) both ways, unicast.
- [ ] The network must allow the shared IP to move between the two servers (ARP updates). In a virtual
      environment (VMware, Hyper-V, cloud), check that MAC spoofing / IP moves are allowed for these VMs.
- [ ] Find out which source IP the firewall uses for EDL downloads. By default it is the management interface.
      If you use a **service route**, it is that interface. You need it only if you limit `FIREWALL_IP`.

### 5.2 Certificate on the firewall

1. Copy `/etc/furnace/tls/furnace.crt` from the leader. It is the public certificate. It is not secret.
2. In PAN-OS: **Device > Certificate Management > Certificates > Import**. Import `furnace.crt`.
   Mark it as **Trusted Root CA**.
3. **Device > Certificate Management > Certificate Profile > Add**. Add `furnace.crt` as the CA (Certificate
   Authority).
4. Commit.

Menu names can differ a little between PAN-OS versions.

### 5.3 EDL objects on the firewall

1. **Objects > External Dynamic Lists > Add**:
   - Name: for example `Furnace-IP-Blocklist`
   - Type: **IP List**
   - Source: `https://SHARED_IP/edl/blocklist.txt`
   - Certificate Profile: the profile from 5.2
   - Client Authentication: the EDL login from setup (user `paloalto` and its password)
   - Check for updates: **Every five minutes**
2. Add a second EDL:
   - Type: **Domain List**
   - Source: `https://SHARED_IP/edl/domains.txt`
   - Same certificate profile, EDL login and update interval.
   - Furnace stores exact domains. Decide whether you want the firewall to also match sub-domains.
3. Use the IP list in security policy rules (deny, source and/or destination).
   Use the domain list in an Anti-Spyware profile (DNS policies) or as your design requires.
4. Commit. Check that the firewall downloaded the lists (in the EDL object, "Test Source URL", or on the CLI:
   `request system external-list show type ip name Furnace-IP-Blocklist`).
5. Check your firewall model's EDL limit. Your limit is 50,000 entries per list. Furnace refuses new adds when a
   list is full (`limit_reached`).

If both Furnace servers are down, the firewall keeps using the last list it downloaded.

### 5.4 XSOAR

- [ ] Store the API tokens (`xsoar`, and `xsoar-whitelist` if you use the whitelist) in the XSOAR credentials
      store.
- [ ] Make XSOAR trust `furnace.crt` (see [6.10](#610-trusting-the-self-signed-certificate)).
- [ ] Build the HTTP calls with the API guide below. Always call the **shared IP**.
- [ ] Test with an IP from `203.0.113.0/24` first only if `FURNACE_ALLOW_TEST_RANGES=true`. In production, this is
      `false`, so test ranges are refused with `documentation_range`. That refusal is also a good first test.

### 5.5 After both servers are installed

- [ ] Run the failover test on the leader: `sudo furnace failover-test`.
- [ ] Check the cleanup timer runs only on the server with the shared IP: `systemctl status furnace-cleanup.timer`.
- [ ] Keep `/root/furnace-peer-bundle.tar.gz` safe or delete it.

---

## 6. API guide for the XSOAR engineer

Furnace does not need an XSOAR integration or pack. XSOAR calls the API with plain HTTPS requests.

### 6.1 Basics

- Base address: `https://SHARED_IP` (always the shared IP, never a server's own IP).
- Format: JSON (JavaScript Object Notation) in and out. Send `Content-Type: application/json`.
- Authentication: the header `Authorization: Bearer <token>`. Every `/api/` call needs it.
  - A token with the scope **block** (setup makes `xsoar`) for `/api/v1/ips` and `/api/v1/domains`.
  - A token with the scope **whitelist** (setup makes `xsoar-whitelist`) for `/api/v1/whitelist/...`.
  - A token with the scope **all** may do both (make one with `sudo furnace token add NAME --scope all`).
- Wrong tokens: after 10 wrong tokens in 15 minutes, the calling address is locked for 15 minutes (`429`
  `locked`), even with a good token. A missing token does not count.
- By default every address may call the API. If `XSOAR_IP` is limited, other addresses get `403` from nginx.
- Rate limit: 10 requests per second per caller (burst 50). Above that: `429`. Wait and try again.

The curl examples below use these shell variables:

```bash
FURNACE=https://192.0.2.10          # the shared IP
TOKEN='paste-the-xsoar-token-here'
WL_TOKEN='paste-the-xsoar-whitelist-token-here'
CA=furnace.crt                       # the Furnace certificate file
```

### 6.2 Fields XSOAR should send

| Field | Required | Max length | What to send |
|---|---|---|---|
| `ip` or `domain` | yes | - | The indicator. One IP or one exact domain. |
| `added_by` | recommended | 100 | The analyst's name or user name (who clicked the button). |
| `reason` | recommended | 500 | Short reason, for example "Phishing C2 server". |
| `incident_id` | recommended | 100 | The XSOAR incident number. Text or number. |

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
| 401 | `unauthorized` | The token is missing or wrong for this endpoint. |
| 403 | (nginx page) | `XSOAR_IP` or `FIREWALL_IP` is limited, and the caller's IP is not on it. |
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
| `field_too_long` | `reason`, `added_by` or `incident_id` is too long, or not text. |

How XSOAR should read an answer:

- `201` -> tell the analyst "blocked".
- `200` with `already_exists` -> tell the analyst "already blocked since <date>" (it is in `message` and `entry.added_at`).
- `422` -> show `message`. Do not retry. The input must change.
- `503`, `429` or a network error -> retry a few times with a pause. If it still fails, tell the analyst.
- `401` -> the token in XSOAR is wrong. Alert the XSOAR admin. Do not retry: wrong tokens lead to a lockout.
- `429` with `locked` -> fix the token, then ask the Furnace admin for `sudo furnace unlock <XSOAR IP>`.

### 6.4 Add an IP

`POST /api/v1/ips` (block token)

```bash
curl --cacert "$CA" -X POST "$FURNACE/api/v1/ips" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"ip":"203.0.113.45","reason":"Phishing C2 server","added_by":"analyst.one","incident_id":"4521"}'
```

Answer `201`:

```json
{"status": "added", "message": "The IP is now blocked.",
 "entry": {"ip": "203.0.113.45", "added_at": "2026-10-26T09:15:02Z", "added_by": "analyst.one",
           "reason": "Phishing C2 server", "incident_id": "4521"}}
```

Answer `200` when it was already there:

```json
{"status": "already_exists",
 "message": "This IP is already blocked since 2026-10-20T08:00:00Z. Nothing changed.",
 "entry": {"ip": "203.0.113.45", "added_at": "2026-10-20T08:00:00Z", "...": "..."}}
```

If the IP was in the archive, it comes back with a new date: `201`, `"restored_from_archive": true`.

Answer `422`:

```json
{"status": "rejected", "message": "This is a private IP address. Only public IPs can be blocked.",
 "reason_code": "private_ip"}
```

### 6.5 Remove an IP

`DELETE /api/v1/ips/<ip>` (block token)

```bash
curl --cacert "$CA" -X DELETE "$FURNACE/api/v1/ips/203.0.113.45?removed_by=analyst.one" \
  -H "Authorization: Bearer $TOKEN"
```

Answers: `200` `{"status": "removed", ...}` or `404` `{"status": "not_found", ...}`.

### 6.6 Look up an IP or a domain

`GET /api/v1/ips/<ip>` and `GET /api/v1/domains/<domain>` (block token)

```bash
curl --cacert "$CA" "$FURNACE/api/v1/ips/203.0.113.45" -H "Authorization: Bearer $TOKEN"
```

```json
{"status": "found", "message": "This IP is blocked.", "list": "active",
 "entry": {"ip": "203.0.113.45", "added_at": "2026-10-26T09:15:02Z", "added_by": "analyst.one",
           "reason": "Phishing C2 server", "incident_id": "4521"},
 "whitelisted_by": null}
```

`list` is `active` (blocked now) or `archive` (was blocked, expired). `404` means it is in neither.
`whitelisted_by` shows the whitelist entry that covers the IP, if any.

```bash
curl --cacert "$CA" "$FURNACE/api/v1/domains/bad.example.com" -H "Authorization: Bearer $TOKEN"
```

### 6.7 Add and remove a domain

`POST /api/v1/domains` and `DELETE /api/v1/domains/<domain>` (block token)

```bash
curl --cacert "$CA" -X POST "$FURNACE/api/v1/domains" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"domain":"bad.example.com","reason":"Malware download site","added_by":"analyst.one","incident_id":"4522"}'

curl --cacert "$CA" -X DELETE "$FURNACE/api/v1/domains/bad.example.com?removed_by=analyst.one" \
  -H "Authorization: Bearer $TOKEN"
```

Domains are stored in lower case. Domains never expire. They stay until someone removes them.

### 6.8 Whitelist: add, remove, list

IPs and ranges: `/api/v1/whitelist/ips`. Domains: `/api/v1/whitelist/domains`. Whitelist token.

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

### 6.9 Bulk whitelist upload

`POST /api/v1/whitelist/ips/bulk` and `POST /api/v1/whitelist/domains/bulk` (whitelist token).
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
   - XSOAR: add it as a trusted custom certificate for the server or engine that makes the call.
     The steps depend on your XSOAR version. Look for "custom certificates" or "trusted certificates" in the
     XSOAR documentation.

The certificate is valid for 10 years.

### 6.11 Health check

`GET /health` needs no token. nginx allows it only from the servers themselves (localhost and the two server
IPs). XSOAR cannot reach it. It is for keepalived and the admins.

```json
{"status": "ok", "version": "0.3.0", "role": "leader", "ips": 1234, "domains": 56}
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
sudo furnace cleanup --dry-run           # what the nightly job would archive
sudo furnace cleanup [--force]           # run it now (--force skips the safety stop)
sudo furnace export ips -o /tmp/ips.csv  # also: archive, domains, whitelist-ips, whitelist-domains, audit
sudo furnace check-config                # show the settings (no secrets)
```

### 7.1 API tokens, EDL logins and the lockout

API tokens and EDL logins are in the database. Furnace keeps only a hash (SHA-256 for tokens, SHA-512 crypt for
passwords). A new token or password is shown **once**. Lost one? Make a new one with `rotate`.
Changes run on the leader. The follower gets them with the next replication (within a minute).
Every change is in the audit log, with the admin's name, never with the secret.

```bash
sudo furnace token list                       # names, scopes and a short id, never the tokens
sudo furnace token add ui --scope all         # a new token: scope block (default), whitelist or all
sudo furnace token rotate xsoar               # a new value; the old one stops working at once
sudo furnace token remove ui                  # it stops working at once

sudo furnace edl-user list                    # the EDL logins (names only)
sudo furnace edl-user add paloalto-2          # a new login with a random password
sudo furnace edl-user rotate paloalto         # a new password; put it in the firewall's EDL objects
sudo furnace edl-user remove paloalto-2

sudo furnace lockouts                         # addresses locked after too many wrong tokens
sudo furnace unlock 198.51.100.20             # unlock one address (or: --all)
```

Give every program its own token (for example `xsoar` and, later, `ui`). Then you can replace or delete one
without touching the others. The audit log shows which token made each change (`source` = `api:NAME@IP`).

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
- IPs with a date older than `ARCHIVE_AFTER_DAYS` go straight to the archive.
  With `--old-ips fresh` they go into the active list with a new date instead. Their expiry is spread out, like
  undated IPs, so they do not all expire on the same night.
- IPs with no date get their expiry spread evenly over the next `ARCHIVE_AFTER_DAYS` days.
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
- Already active: nothing changes, the old date is kept, and the answer says so.
- In the archive and added again: it moves back to the active list with a new date. An IP is never in both.
- Stored for each IP: date added, who added it, reason, XSOAR incident number.

**Domains**
- Exact domains only. Refused: wildcards, URLs, IPs, invalid names. Stored in lower case.
  Non-ASCII names are stored as punycode (`xn--...`).
- No expiry and no archive.
- Refused when on the domain whitelist. The domain whitelist is exact: whitelisting `example.com` does not
  whitelist `www.example.com`.

**Whitelist**
- IPs and CIDR (Classless Inter-Domain Routing) ranges, and exact domains.
- Ranges shorter than /8 (IPv4) or /16 (IPv6) are refused.
- Adding an entry removes the blocked IPs or domains it covers, with an audit entry.
- Look-ups use merged, sorted ranges and binary search. Checking 30,000 IPs against thousands of ranges takes
  milliseconds.

## 9. The nightly cleanup and the archive

- `furnace-cleanup.timer` runs every night at 02:30, only on the server that owns the shared IP.
- It moves IPs older than `ARCHIVE_AFTER_DAYS` to the archive table in one transaction, rebuilds `blocklist.txt`,
  and rewrites `/var/lib/furnace/archive/archive.csv`.
- The archive CSV is only storage. It is not an EDL. nginx never serves it.
- **Safety stop:** if one run would remove more than 20 percent of the list, and the list has at least 100 IPs,
  the job changes nothing, writes an audit entry and exits with code 3. systemd marks the run as failed.
  Check the dates. If the removal is correct, run `sudo furnace cleanup --force`.
- `sudo furnace cleanup --dry-run` shows what would move.
- Logs: `journalctl -u furnace-cleanup`.

## 10. Two servers: leader and follower

- keepalived holds the **shared IP** on the current leader. It uses unicast VRRP between the two servers.
- Both servers use state `BACKUP` with `nopreempt`. A server that comes back does **not** take the IP back.
  Priority 150 is the preferred leader at the very first start, 100 is the follower.
- keepalived calls `/health` every 5 seconds. After 3 failures the IP moves, even if the machine is still alive.
- When a server takes the shared IP, the notify script marks it as leader and starts the cleanup timer.
  When it loses the IP, it marks it as follower and stops the timer.
- **Replication:** every minute the leader makes a SQLite backup snapshot and pushes it over SSH to the follower,
  with a dedicated key. The key can only run the receive command. The follower loads the snapshot and rebuilds its
  EDL files.
- The follower refuses all changes while it is not the leader (`503 not_leader`).

### 10.1 Risks

- **Split brain.** If the two servers cannot see each other but both are alive, both take the shared IP.
  Each one accepts changes, and neither takes snapshots from the other. When the link comes back, one keeps the IP.
  Changes made on the other server during the split are overwritten by the next snapshot. Check the audit log of
  both servers after a network problem. Keep VRRP and SSH between the servers on a reliable path.
- **Up to one minute of lost changes.** If the leader dies, changes from its last minute may not have reached the
  follower. XSOAR got a success answer for them. Ask analysts to re-click after a failover, or check the audit log.
  The duplicate rule makes a re-click harmless.
- **Both servers down.** The firewall keeps the last list it downloaded and keeps blocking. New blocks are not
  possible until a server is back.
- Replication sends the whole database every minute. For large audit logs, this is more network traffic.

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
| `/lib/systemd/system/furnace-*` | The systemd units. |
| `/etc/furnace/setup.conf` | Your setup answers. Readable by root only. |
| `/etc/furnace/furnace.env` | Program settings (no secrets). |
| `/etc/furnace/tls/` | Certificate and private key. |
| `/etc/furnace/ssh/` | Replication key. |
| `/var/lib/furnace/furnace.db` | The database (master record), with the API tokens and EDL logins as hashes. |
| `/var/lib/furnace/lockout.db` | Addresses locked after wrong tokens (this server only). |
| `/var/lib/furnace-auth/edl-users` | EDL logins for nginx (name and hash), written from the database. Never served. |
| `/var/lib/furnace/archive/archive.csv` | Archive storage. Never served. |
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
sudo apt install ./furnace_0.3.0_all.deb
```

The package runs `furnace setup --upgrade` by itself. It uses the saved answers and asks no questions.
Data, tokens and the certificate are kept. The API is reloaded without downtime, and keepalived is restarted only
if its settings changed, so an upgrade does not move the shared IP. If you change answers that affect keepalived
(for example the VRRP router ID), the restart can move the shared IP to the other server. That is safe.

**From 0.2 to 0.3:** do the **leader first**, then the follower.
- The API tokens of 0.2 keep working. They move from `/etc/furnace/tokens.env` into the database (names `xsoar`
  and `xsoar-whitelist`), and the file is deleted.
- The lists now need a login. The upgrade makes the login `paloalto` and writes its password to
  `/root/furnace-new-secrets.txt`. Until you add it to the firewall's EDL objects, the firewall cannot download new
  lists and keeps the last one it has. Delete the file afterwards.
- Your saved `XSOAR_IP` and `FIREWALL_IP` stay. To open them to every address, run `sudo furnace setup` on both
  servers and answer `any`.

If the upgrade step reports a problem, the package stays installed. Fix the problem and run
`sudo furnace setup --upgrade`. The details are in `/var/log/furnace-setup.log`.

**Uninstall:**

```bash
sudo apt remove furnace    # removes the program and stops the services, keeps the data, tokens and logins
sudo apt purge furnace     # also deletes the database, backups, tokens, logins, certificate and users
```

`apt remove` also removes the Furnace nginx site, the keepalived configuration (the shared IP is released) and the
Furnace ufw rules. ufw itself stays on. Installing the package again later uses the kept data.

## 13. Tests

Unit tests (run on Linux, because the file lock uses `fcntl`):

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

They cover validation, the duplicate rule, restore from the archive, the cleanup with fake dates and the
`ARCHIVE_AFTER_DAYS` setting, the safety stop, the whitelist, imports (dated and undated, dry-run, formats), the
API (token scopes, rotate and remove, the lockout, the setup self-test token, errors), EDL logins (checked with
openssl), the token migration from 0.2, a snapshot from an older leader, the token commands, many requests at the same time (threads and processes), and that
no temporary files are left behind.

Integration test with Docker (two servers, a fake firewall and a fake XSOAR; only test addresses).
It builds the `.deb`, installs it with apt and answers the setup questions through a terminal:

```bash
tests/integration/run.sh 24.04
tests/integration/run.sh 22.04
```

All tests use only 192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24, the IPv6 documentation range 2001:db8::/32
(for volume tests only) and names under `example.com`, `example.net` and `example.org`.

The integration test passed 91 of 91 checks on Ubuntu 22.04 and on Ubuntu 24.04 (v0.3.0). It covers:
building the package, `apt install` with its dependencies, nothing running before setup, `setup --check`, refusing
bad settings, a follower without a bundle, the leader setup by answering the questions (including a wrong answer
that is asked again, the card and IP found automatically, the keepalived password created), the follower setup with
only two questions, certificate and settings copied through the bundle, the secrets shown once and never in the
log or the database, the self-test, API and EDL access through the shared IP, the EDL login (no login or a wrong
password is refused), token scopes, the token commands, the wrong-token lockout and unlock, tokens and logins
replicated to the follower, nginx and ufw rules with limited IPs and with `any` (archive and /health never served),
replication both ways, the follower refusing changes, `furnace status`, an import from a file only root can read,
a package upgrade that keeps data and tokens without moving the shared IP, both failover modes, the cleanup timer
moving with the shared IP, `apt remove` (data kept) and `apt purge` (data deleted).

The unit tests also pass with Ubuntu's own Flask packages: Flask 2.0.1 on 22.04 and Flask 3.0.2 on 24.04.

## 14. What was not tested

Be aware of these limits before you go live:

- **A real Palo Alto firewall.** The EDL download, also with the EDL login, was tested with curl from a container.
  The PAN-OS menus in this README come from documentation and may differ in your version.
- **A real XSOAR server.** The API calls were tested with curl. XSOAR's own way to trust a certificate was not
  tested.
- **keepalived on real servers or VMs.** It was tested only in Docker containers on one host. Real switches,
  VMware/Hyper-V port security, ARP behaviour and a real network outage (split brain) were not tested.
- **A real power cut.** SQLite `synchronous=FULL` and atomic renames are designed for this, but it was not tested.
- **Long running time.** No test ran for days. Audit log growth and backup rotation over months were not tested.
- **Large real lists with the firewall.** 30,000-line imports were timed (about 2 seconds), but not loaded by a
  firewall.
- **A server without the Ubuntu package mirror.** apt needs the mirror, a proxy or a local mirror for the
  dependencies. An install without any of them was not tested.
- **`furnace setup` over a real SSH session.** The questions were answered through a terminal in a container,
  not by a person over SSH.
- **An upgrade from 0.2 on real servers.** The token migration was tested in unit tests, not with a real 0.2
  install.
- **`USE_HTTPS=no`** (HTTP test mode; the EDL password would travel in clear text), `HOST_FIREWALL=none` and `furnace setup --config` for a follower were
  not run in the integration test.
