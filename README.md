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
4. [Settings in config.env](#4-settings-in-configenv)
5. [Checklist of manual work](#5-checklist-of-manual-work)
6. [API guide for the XSOAR engineer](#6-api-guide-for-the-xsoar-engineer)
7. [Daily admin work (furnace-admin)](#7-daily-admin-work-furnace-admin)
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

Technology: Ubuntu 22.04 or 24.04, Python 3 with Flask, gunicorn, SQLite in WAL mode (Write-Ahead Logging) with
`synchronous=FULL`, nginx, systemd and keepalived.

## 2. What you need

- Two Ubuntu servers (22.04 or 24.04) in the same network segment. One is enough for a lab.
- Three IP addresses in that segment: one per server and one **shared IP** (VIP, virtual IP).
- The servers need packages from the Ubuntu repositories and from PyPI (the Python Package Index),
  or a proxy. Offline install: put wheel files in a folder named `wheels` next to `install.sh`.
- The IP address the firewall uses to download EDLs, and the IP address of XSOAR.

## 3. Install

Install the **leader** first, then the **follower**.

### 3.1 Leader

1. Copy the whole `furnace` folder to the server, for example to `/root/furnace`.
2. Create the settings file:
   ```bash
   cd /root/furnace
   cp config.env.example config.env
   nano config.env          # set ROLE=leader and your real IPs
   ```
3. See what the install would do. This changes nothing:
   ```bash
   sudo ./install.sh --check
   ```
4. Install:
   ```bash
   sudo ./install.sh
   ```
5. The script shows the two **API tokens once** at the end. Store them in the XSOAR credentials store.
   They are not written to the install log. You can read them later with `sudo cat /etc/furnace/tokens.env`.
6. The script writes `/root/furnace-peer-bundle.tar.gz`. It holds the tokens, the certificate and the replication
   key. It is secret. You copy it to the follower in the next step.

### 3.2 Follower

1. Copy the `furnace` folder to the second server.
2. Create `config.env`. Use `ROLE=follower`. Swap `THIS_IP` and `PEER_IP`. All other values stay the same,
   including `KEEPALIVED_PASSWORD` and `VRRP_ROUTER_ID`.
3. Copy the bundle from the leader into the `furnace` folder (or your home folder) on the follower:
   ```bash
   # on the leader
   sudo scp /root/furnace-peer-bundle.tar.gz you@FOLLOWER_IP:~/
   ```
4. Install:
   ```bash
   sudo ./install.sh
   ```
   The script copies the secrets from the bundle and then deletes the bundle file on the follower.
5. Delete the bundle copy wherever else you put it. Keep the one in `/root` on the leader only if you want to
   reinstall the follower later.

### 3.3 What the install script does

- Stops at the first error (strict mode) and tells you what went wrong.
- Checks that it runs as root on Ubuntu 22.04 or 24.04.
- Checks every value in `config.env` and stops with a clear message if one is wrong.
- Installs the packages: `python3-venv nginx keepalived openssl curl ufw openssh-server sqlite3`.
- Creates the user `furnace` (no login) and the user `furnace-sync` (can only receive replication snapshots).
- Creates a Python virtual environment in `/opt/furnace/venv` and installs Furnace into it.
- Leader: creates both API tokens, a self-signed certificate (10 years, names the shared IP and both server IPs)
  and the replication SSH key. Follower: copies them from the bundle.
- Writes the nginx, systemd and keepalived files from the templates in `deploy/`.
- Opens only the needed ports on the host firewall (ufw): HTTPS, SSH, and VRRP (protocol 112) between the servers.
- Starts everything and runs a **self-test**: it adds the fake IP `192.0.2.123`, checks that nginx serves it in
  `blocklist.txt`, removes it, and checks that it is gone. It also checks that the archive and the database are
  not served, and that the API refuses calls without a token.
- Prints a summary of what worked and what you still must do by hand.

**Safe to run again.** A second run upgrades the program. It never wipes the database and never changes the
tokens or the certificate. The log is in `/var/log/furnace-install.log`. It never contains secrets.

## 4. Settings in config.env

| Setting | Example | Meaning |
|---|---|---|
| `ROLE` | `leader` | `leader` (preferred leader, keepalived priority 150) or `follower` (priority 100). |
| `THIS_IP` | `192.0.2.11` | This server's IP. It must be set on `INTERFACE`. |
| `PEER_IP` | `192.0.2.12` | The other Furnace server. |
| `SHARED_IP` | `192.0.2.10` | The shared IP. The firewall and XSOAR always use this address. |
| `SHARED_IP_PREFIX` | `24` | Network prefix of the shared IP. |
| `INTERFACE` | `eth0` | Network card name. See `ip -br addr`. |
| `XSOAR_IP` | `198.51.100.20` | XSOAR address(es) that may call the API. Comma-separated list allowed. |
| `FIREWALL_IP` | `198.51.100.1` | Address(es) the firewall downloads from. For a firewall HA pair, list both. |
| `ARCHIVE_AFTER_DAYS` | `30` | Days an IP stays active before the nightly job archives it. |
| `KEEPALIVED_PASSWORD` | (6-8 chars) | VRRP (Virtual Router Redundancy Protocol) password. Same on both servers. |
| `VRRP_ROUTER_ID` | `51` | 1-255. Same on both servers. Must be unique in the network segment. |
| `USE_HTTPS` | `yes` | `yes` = HTTPS (default). `no` = plain HTTP, for a test lab only. |
| `MAX_IPS` / `MAX_DOMAINS` | `50000` | The firewall's EDL limit. Adds beyond it are refused. 0 = no limit. |
| `SSH_ALLOW_FROM` | `any` | Who may use SSH. `any` or a list of IPs. The other server is always allowed. |
| `HOST_FIREWALL` | `ufw` | `ufw` = configure the host firewall. `none` = leave it alone. |

The program settings live in `/etc/furnace/furnace.env` (written by the install). They all start with `FURNACE_`:
`FURNACE_ARCHIVE_AFTER_DAYS`, `FURNACE_MAX_IPS`, `FURNACE_MAX_DOMAINS`, `FURNACE_BACKUP_KEEP` (14),
`FURNACE_CLEANUP_MAX_PERCENT` (20), `FURNACE_CLEANUP_MIN_SIZE` (100), `FURNACE_ALLOW_TEST_RANGES` (false).
The tokens `FURNACE_BLOCK_TOKEN` and `FURNACE_WHITELIST_TOKEN` are in `/etc/furnace/tokens.env`.
If `FURNACE_WHITELIST_TOKEN` is empty, the block token is used for the whitelist too.
Tokens must be at least 20 characters. The API does not start without a block token.

## 5. Checklist of manual work

The install cannot do these things for you.

### 5.1 Network access

- [ ] Firewall (EDL download source address) -> shared IP, TCP 443.
- [ ] XSOAR (and any XSOAR engines) -> shared IP, TCP 443.
- [ ] Leader <-> follower: SSH (TCP 22) both ways, for replication.
- [ ] Leader <-> follower: VRRP (IP protocol 112) both ways, unicast.
- [ ] The network must allow the shared IP to move between the two servers (ARP updates). In a virtual
      environment (VMware, Hyper-V, cloud), check that MAC spoofing / IP moves are allowed for these VMs.
- [ ] Find out which source IP the firewall uses for EDL downloads. By default it is the management interface.
      If you use a **service route**, it is that interface. Put that IP in `FIREWALL_IP`.

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
   - Check for updates: **Every five minutes**
2. Add a second EDL:
   - Type: **Domain List**
   - Source: `https://SHARED_IP/edl/domains.txt`
   - Same certificate profile and update interval.
   - Furnace stores exact domains. Decide whether you want the firewall to also match sub-domains.
3. Use the IP list in security policy rules (deny, source and/or destination).
   Use the domain list in an Anti-Spyware profile (DNS policies) or as your design requires.
4. Commit. Check that the firewall downloaded the lists (in the EDL object, "Test Source URL", or on the CLI:
   `request system external-list show type ip name Furnace-IP-Blocklist`).
5. Check your firewall model's EDL limit. Your limit is 50,000 entries per list. Furnace refuses new adds when a
   list is full (`limit_reached`).

If both Furnace servers are down, the firewall keeps using the last list it downloaded.

### 5.4 XSOAR

- [ ] Store the block token (and the whitelist token, if used) in the XSOAR credentials store.
- [ ] Make XSOAR trust `furnace.crt` (see [6.10](#610-trusting-the-self-signed-certificate)).
- [ ] Build the HTTP calls with the API guide below. Always call the **shared IP**.
- [ ] Test with an IP from `203.0.113.0/24` first only if `FURNACE_ALLOW_TEST_RANGES=true`. In production, this is
      `false`, so test ranges are refused with `documentation_range`. That refusal is also a good first test.

### 5.5 After both servers are installed

- [ ] Run the failover test on the leader: `sudo /opt/furnace/bin/furnace-failover-test`.
- [ ] Check the cleanup timer runs only on the server with the shared IP: `systemctl status furnace-cleanup.timer`.
- [ ] Keep `/root/furnace-peer-bundle.tar.gz` safe or delete it.

---

## 6. API guide for the XSOAR engineer

Furnace does not need an XSOAR integration or pack. XSOAR calls the API with plain HTTPS requests.

### 6.1 Basics

- Base address: `https://SHARED_IP` (always the shared IP, never a server's own IP).
- Format: JSON (JavaScript Object Notation) in and out. Send `Content-Type: application/json`.
- Authentication: the header `Authorization: Bearer <token>`.
  - **Block token** for `/api/v1/ips` and `/api/v1/domains`.
  - **Whitelist token** for `/api/v1/whitelist/...`. If no whitelist token is set, use the block token.
- Only the XSOAR IP(s) may call `/api/`. Others get `403` from nginx.
- Rate limit: 10 requests per second per caller (burst 50). Above that: `429`. Wait and try again.

The curl examples below use these shell variables:

```bash
FURNACE=https://192.0.2.10          # the shared IP
TOKEN='paste-the-block-token-here'
WL_TOKEN='paste-the-whitelist-token-here'
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
| 403 | (nginx page) | The caller's IP may not use this address. |
| 404 | `not_found` | The entry is not in the list (or the address does not exist). |
| 405 | `method_not_allowed` | Wrong HTTP method. |
| 413 | `too_large` | The body is larger than 20 MB. |
| 422 | `rejected` | Refused. `reason_code` says why (table below). |
| 429 | (nginx page) | Too many requests. Slow down. |
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
- `401` -> the token in XSOAR is wrong. Alert the XSOAR admin.

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
{"status": "ok", "version": "0.1.0", "role": "leader", "ips": 1234, "domains": 56}
```

---

## 7. Daily admin work (furnace-admin)

Run the admin command with sudo on the server that owns the shared IP. It runs as the `furnace` user.
Files you import must be readable by that user, for example in `/tmp` or `/var/lib/furnace/reports`.

```bash
sudo furnace-admin stats                       # counts, dates, sizes, role
sudo furnace-admin rebuild                     # rebuild blocklist.txt, domains.txt and archive.csv from the database
sudo furnace-admin backup [--keep N]           # SQLite backup, keeps the newest N (default 14)
sudo furnace-admin cleanup --dry-run           # what the nightly job would archive
sudo furnace-admin cleanup [--force]           # run it now (--force skips the safety stop)
sudo furnace-admin export ips -o /tmp/ips.csv  # also: archive, domains, whitelist-ips, whitelist-domains, audit
sudo furnace-admin check-config                # show the settings (never the tokens)
```

A database backup also runs every day at 01:30 (`furnace-backup.timer`).

### 7.1 Import IPs

```bash
sudo furnace-admin import-ips /tmp/old-list.csv --dry-run     # check first, change nothing
sudo furnace-admin import-ips /tmp/old-list.csv
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

### 7.2 Import domains and whitelists

```bash
sudo furnace-admin import-domains /tmp/domains.txt [--dry-run]            # domain or domain,reason
sudo furnace-admin import-whitelist-ips /tmp/wl-ips.txt [--dry-run]       # IPs and CIDR ranges
sudo furnace-admin import-whitelist-domains /tmp/wl-domains.txt [--dry-run]
```

## 8. Rules for IPs, domains and the whitelist

**IPs**
- Single public IPs only (IPv4 and IPv6).
- Refused: ranges, private, loopback, link-local, shared (100.64.0.0/10), multicast, reserved.
- The documentation ranges 192.0.2.0/24, 198.51.100.0/24 and 203.0.113.0/24 are refused unless
  `FURNACE_ALLOW_TEST_RANGES=true` (tests and the install self-test only).
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
  Check the dates. If the removal is correct, run `sudo furnace-admin cleanup --force`.
- `sudo furnace-admin cleanup --dry-run` shows what would move.
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
sudo /opt/furnace/bin/furnace-failover-test        # stops keepalived here (like a dead machine)
sudo /opt/furnace/bin/furnace-failover-test --web  # stops nginx here (the web service breaks)
```

It stops the service on purpose, waits for the other server to take the shared IP, checks that it answers on the
shared IP as the leader, and then starts the service again. Afterwards the other server stays the leader.

## 11. Files and folders

| Path | What |
|---|---|
| `/opt/furnace/venv` | The program (Python virtual environment). |
| `/opt/furnace/bin` | Helper scripts (keepalived, replication, failover test). |
| `/usr/local/bin/furnace-admin` | The admin command. |
| `/etc/furnace/furnace.env` | Settings (no secrets). |
| `/etc/furnace/tokens.env` | API tokens. Readable only by `furnace`. |
| `/etc/furnace/tls/` | Certificate and private key. |
| `/etc/furnace/ssh/` | Replication key. |
| `/var/lib/furnace/furnace.db` | The database (master record). |
| `/var/lib/furnace/archive/archive.csv` | Archive storage. Never served. |
| `/var/lib/furnace/backups/` | Database backups. |
| `/var/lib/furnace/reports/` | Import reports. |
| `/var/lib/furnace-edl/` | `blocklist.txt` and `domains.txt` (served by nginx). |
| `/var/lib/furnace-incoming/` | Snapshots received from the other server. |
| `/run/furnace-state/role` | `leader` or `follower` (written by keepalived). |
| `/var/log/furnace-install.log` | Install log. |

Services: `furnace-api` (gunicorn), `nginx`, `keepalived`. Timers: `furnace-cleanup`, `furnace-backup`,
`furnace-replicate`. Path unit: `furnace-apply-replica`.
Logs: `journalctl -u furnace-api`, `/var/log/nginx/furnace-access.log`.

## 12. Upgrade and uninstall

**Upgrade:** copy the new `furnace` folder to the server (keep your `config.env`) and run `sudo ./install.sh`
again. Do the follower first, then the leader. Data, tokens and the certificate are kept.

**Uninstall:**

```bash
sudo /opt/furnace/uninstall.sh            # removes the program and services, keeps the data
sudo /opt/furnace/uninstall.sh --purge    # also deletes the database, backups, tokens, certificate and users
```

## 13. Tests

Unit tests (run on Linux, because the file lock uses `fcntl`):

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

They cover validation, the duplicate rule, restore from the archive, the cleanup with fake dates and the
`ARCHIVE_AFTER_DAYS` setting, the safety stop, the whitelist, imports (dated and undated, dry-run, formats), the
API (tokens, separate whitelist token, errors), many requests at the same time (threads and processes), and that
no temporary files are left behind.

Integration test with Docker (two servers, a fake firewall and a fake XSOAR; only test addresses):

```bash
tests/integration/run.sh 24.04
tests/integration/run.sh 22.04
```

All tests use only 192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24, the IPv6 documentation range 2001:db8::/32
(for volume tests only) and names under `example.com`, `example.net` and `example.org`.

## 14. What was not tested

See the end of the project hand-over notes. In short: a real Palo Alto firewall, a real XSOAR server, keepalived
on real hardware or VMs (it was tested in Docker containers only), and long-running behaviour.
