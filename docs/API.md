# Furnace API reference

This is the full reference for the Furnace HTTP API and the two list files.
The README has the same content in a shorter form ([README section 6](../README.md#6-api-guide)).

## Contents

1. [Basics](#1-basics)
2. [Authentication and tokens](#2-authentication-and-tokens)
3. [Limits](#3-limits)
4. [Answers and status codes](#4-answers-and-status-codes)
5. [Reason codes](#5-reason-codes)
6. [Endpoint summary](#6-endpoint-summary)
7. [Blocked IPs](#7-blocked-ips)
8. [Blocked domains](#8-blocked-domains)
9. [Whitelist: IPs and ranges](#9-whitelist-ips-and-ranges)
10. [Whitelist: domains](#10-whitelist-domains)
11. [Bulk whitelist upload](#11-bulk-whitelist-upload)
12. [List files for the list reader](#12-list-files-for-the-list-reader)
13. [Health check](#13-health-check)
14. [How a caller should handle answers](#14-how-a-caller-should-handle-answers)

---

## 1. Basics

- Base address: `https://SHARED_IP`. Always use the shared IP, never a server's own IP.
  Only the leader accepts changes. The follower answers `503` with `not_leader`.
- Format: JSON in and out. Send `Content-Type: application/json`.
  The only exception is bulk upload, which also accepts `text/plain` ([section 11](#11-bulk-whitelist-upload)).
- HTTPS uses a self-signed certificate. Trust it, do not switch off checks
  ([README section 6.10](../README.md#610-trusting-the-self-signed-certificate)).
- Every API path starts with `/api/v1/`.

The curl examples use these shell variables:

```bash
FURNACE=https://192.0.2.10          # the shared IP
TOKEN='paste-the-block-token-here'
WL_TOKEN='paste-the-whitelist-token-here'
CA=furnace.crt                       # the Furnace certificate file
```

## 2. Authentication and tokens

Send the token in a header on every `/api/` call:

```
Authorization: Bearer <token>
```

Each token has a scope:

| Scope | May call |
|---|---|
| `block` | `/api/v1/ips...` and `/api/v1/domains...` |
| `whitelist` | `/api/v1/whitelist/...` |
| `all` | Everything |

Setup makes two tokens: `block` (scope block) and `whitelist` (scope whitelist).
Manage tokens on the leader:

```bash
sudo furnace token list
sudo furnace token add NAME --scope block|whitelist|all   # the token is shown once
sudo furnace token rotate NAME                            # new value, the old one stops at once
sudo furnace token remove NAME
```

Tokens are stored as SHA-256 hashes and are copied to the follower.

**Lockout.** After 10 wrong tokens in 15 minutes, the calling address is locked for 15 minutes.
While it is locked, every call gets `429` `locked`, even with a good token. The `Retry-After` header gives the
seconds to wait. A missing token does not count as wrong. Unlock early with `sudo furnace unlock <caller IP>`.

**A good token with the wrong scope** gets `401`. It does not count towards the lockout.

**Address rules (optional).** By default any address may call the API. If `API_ALLOW_FROM` is set, other
addresses get a `403` page from nginx (not JSON).

## 3. Limits

| Limit | Value | What happens above it |
|---|---|---|
| API rate | 10 requests per second per address, burst 50 | `429` page from nginx |
| List file rate | 2 requests per second per address, burst 10 | `429` page from nginx |
| Request body | 20 MB | `413` `too_large` |
| Bulk entries | 200,000 per request | `400` `bad_request` |
| `added_by`, `removed_by`, `incident_id` | 100 characters | `422` `field_too_long` |
| `reason` | 500 characters | `422` `field_too_long` |
| Whitelist list page | `limit` 1 to 10,000 (default 1,000) | Clamped to the range |
| Blocked IPs | `MAX_IPS` setting (the list reader's limit) | `422` `limit_reached` |

## 4. Answers and status codes

Every JSON answer has `status` and a plain English `message`. Show `message` to the person.
Some answers have more fields (described for each endpoint).

| HTTP | `status` | Meaning |
|---|---|---|
| 201 | `added` | New entry. It is now blocked (or whitelisted). |
| 200 | `already_exists` | It was already there. Nothing changed. The old date is kept. |
| 200 | `removed` | The entry was removed. |
| 200 | `found` | Look-up result. |
| 200 | `ok` | List or bulk result. |
| 400 | `bad_request` | The body is not a JSON object, or a field has the wrong type. |
| 401 | `unauthorized` | The token is missing, wrong, or has the wrong scope. |
| 403 | (nginx page) | The caller's address is not allowed (`API_ALLOW_FROM` or `LISTS_ALLOW_FROM`). |
| 404 | `not_found` | The entry is not in the list, or the path does not exist. |
| 405 | `method_not_allowed` | Wrong HTTP method for this path. |
| 413 | `too_large` | The body is larger than 20 MB. |
| 422 | `rejected` | Refused. `reason_code` says why ([section 5](#5-reason-codes)). |
| 429 | (nginx page) | Too many requests. Slow down. |
| 429 | `locked` | Too many wrong tokens from this address. See `Retry-After`. |
| 503 | `rejected` + `reason_code: not_leader` | You reached the follower. Use the shared IP. |
| 503 | `busy` | Another change is running. Try again in a few seconds. |
| 503 | `error` | Health check only: the service is not healthy. |
| 500 | `error` | Unexpected server error. Details are only in the server log. |

Example of a refusal:

```json
{"status": "rejected", "message": "This is a private IP address. Only public IPs can be blocked.",
 "reason_code": "private_ip"}
```

## 5. Reason codes

`reason_code` appears on `422` answers (and on `503` with `not_leader`).

| Code | Endpoint | Meaning |
|---|---|---|
| `missing_value` | all adds | No IP or domain was sent. |
| `invalid_ip` | IPs | Not a valid IP address. |
| `range_not_allowed` | IPs | A range (CIDR or a-b) was sent. Send one IP. |
| `private_ip`, `loopback_ip`, `link_local_ip`, `shared_address_space`, `multicast_ip`, `reserved_ip`, `unspecified_ip`, `not_public`, `ipv4_mapped_not_allowed` | IPs | Not a single public IP. |
| `documentation_range` | IPs | 192.0.2.0/24, 198.51.100.0/24 or 203.0.113.0/24, and test ranges are switched off. |
| `whitelisted` | IPs, domains | The value is on the whitelist. The message names the whitelist entry. |
| `limit_reached` | IPs | The list is full (`MAX_IPS`). |
| `invalid_domain` | domains | Not a valid domain name. |
| `wildcard_not_allowed` | domains | A wildcard like `*.example.com` was sent. |
| `url_not_allowed` | domains | A URL was sent. Send only the domain. |
| `ip_not_allowed` | domains | An IP was sent to a domain endpoint. |
| `invalid_cidr` | whitelist IPs | Not a valid IP or range. |
| `range_too_wide` | whitelist IPs | Shorter than /8 (IPv4) or /16 (IPv6). |
| `field_too_long` | all | `reason`, `added_by`, `removed_by` or `incident_id` is too long, or not text. |
| `not_leader` | all changes | This server is the follower (HTTP 503). |

## 6. Endpoint summary

| Method | Path | Scope | What it does |
|---|---|---|---|
| POST | `/api/v1/ips` | block | Block an IP |
| GET | `/api/v1/ips/<ip>` | block | Look up an IP |
| DELETE | `/api/v1/ips/<ip>` | block | Unblock an IP |
| POST | `/api/v1/domains` | block | Block a domain |
| GET | `/api/v1/domains/<domain>` | block | Look up a domain |
| DELETE | `/api/v1/domains/<domain>` | block | Unblock a domain |
| POST | `/api/v1/whitelist/ips` | whitelist | Whitelist an IP or range |
| GET | `/api/v1/whitelist/ips` | whitelist | List whitelisted IPs and ranges (paged) |
| DELETE | `/api/v1/whitelist/ips/<ip or cidr>` | whitelist | Remove from the whitelist |
| POST | `/api/v1/whitelist/ips/bulk` | whitelist | Bulk whitelist IPs and ranges |
| POST | `/api/v1/whitelist/domains` | whitelist | Whitelist a domain |
| GET | `/api/v1/whitelist/domains` | whitelist | List whitelisted domains (paged) |
| DELETE | `/api/v1/whitelist/domains/<domain>` | whitelist | Remove from the whitelist |
| POST | `/api/v1/whitelist/domains/bulk` | whitelist | Bulk whitelist domains |
| GET | `/edl/blocklist.txt` | list login | Blocked IPs, one per line |
| GET | `/edl/domains.txt` | list login | Blocked domains, one per line |
| GET | `/health` | none (servers only) | Health and role |

## 7. Blocked IPs

### 7.1 Block an IP

`POST /api/v1/ips`

Body:

| Field | Required | Max | What to send |
|---|---|---|---|
| `ip` | yes | - | One public IPv4 or IPv6 address. |
| `reason` | recommended | 500 | Short reason, for example "Phishing C2 server". |
| `added_by` | recommended | 100 | The person who asked for the block. |
| `incident_id` | recommended | 100 | The incident or ticket number. Text or number. |

```bash
curl --cacert "$CA" -X POST "$FURNACE/api/v1/ips" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"ip":"203.0.113.45","reason":"Phishing C2 server","added_by":"analyst.one","incident_id":"4521"}'
```

`201` (new):

```json
{"status": "added", "message": "The IP is now blocked.",
 "entry": {"ip": "203.0.113.45", "added_at": "2026-10-26T09:15:02Z", "added_by": "analyst.one",
           "reason": "Phishing C2 server", "incident_id": "4521"}}
```

If the IP was in the archive, it comes back with a new date and `"restored_from_archive": true`.

`200` (already blocked; the old date is kept):

```json
{"status": "already_exists",
 "message": "This IP is already blocked since 2026-10-20T08:00:00Z. Nothing changed.",
 "entry": {"ip": "203.0.113.45", "added_at": "2026-10-20T08:00:00Z", "...": "..."}}
```

Other answers: `422` `rejected` ([section 5](#5-reason-codes)), `503` `not_leader` or `busy`.

Blocked IPs move to the archive after `ARCHIVE_AFTER_DAYS` (nightly cleanup). They then leave the list file.

### 7.2 Look up an IP

`GET /api/v1/ips/<ip>`

```bash
curl --cacert "$CA" "$FURNACE/api/v1/ips/203.0.113.45" -H "Authorization: Bearer $TOKEN"
```

`200`:

```json
{"status": "found", "message": "This IP is blocked.", "list": "active",
 "entry": {"ip": "203.0.113.45", "added_at": "2026-10-26T09:15:02Z", "added_by": "analyst.one",
           "reason": "Phishing C2 server", "incident_id": "4521"},
 "whitelisted_by": null}
```

- `list` is `active` (blocked now) or `archive` (was blocked, expired; `entry` then also has `archived_at`).
- `whitelisted_by` is the whitelist range that covers the IP, or `null`.
- `404` `not_found` means it is in neither list. `whitelisted_by` is still included.

Look-ups work on the follower too.

### 7.3 Unblock an IP

`DELETE /api/v1/ips/<ip>`

Optional: `removed_by` (max 100) as a query parameter, or in a JSON body.

```bash
curl --cacert "$CA" -X DELETE "$FURNACE/api/v1/ips/203.0.113.45?removed_by=analyst.one" \
  -H "Authorization: Bearer $TOKEN"
```

`200`:

```json
{"status": "removed", "message": "The IP is no longer blocked.", "ip": "203.0.113.45"}
```

`404` `not_found`: the IP is not in the active blocklist.

## 8. Blocked domains

Same as IPs, with the field `domain`. Domains are stored in lower case. They never expire.
Only exact domains are accepted: no wildcards, no URLs, no IPs.

### 8.1 Block a domain

`POST /api/v1/domains`

Body: `domain` (required), `reason`, `added_by`, `incident_id` (same limits as [7.1](#71-block-an-ip)).

```bash
curl --cacert "$CA" -X POST "$FURNACE/api/v1/domains" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"domain":"bad.example.com","reason":"Malware download site","added_by":"analyst.one","incident_id":"4522"}'
```

Answers: `201` `added`, `200` `already_exists`, `422` `rejected`, `503`.

### 8.2 Look up a domain

`GET /api/v1/domains/<domain>`

```bash
curl --cacert "$CA" "$FURNACE/api/v1/domains/bad.example.com" -H "Authorization: Bearer $TOKEN"
```

Answers: `200` `found` with `entry`, or `404` `not_found`.

### 8.3 Unblock a domain

`DELETE /api/v1/domains/<domain>` (optional `removed_by`)

```bash
curl --cacert "$CA" -X DELETE "$FURNACE/api/v1/domains/bad.example.com?removed_by=analyst.one" \
  -H "Authorization: Bearer $TOKEN"
```

Answers: `200` `removed` or `404` `not_found`.

## 9. Whitelist: IPs and ranges

A whitelist entry protects an IP or range. Furnace refuses to block anything it covers (`whitelisted`).
When you add an entry, Furnace also removes every blocked IP it covers. The answer lists them in
`removed_from_blocklist`, and each removal is in the audit log.

### 9.1 Add

`POST /api/v1/whitelist/ips`

Body:

| Field | Required | Max | What to send |
|---|---|---|---|
| `entry` (or `cidr`, or `ip`) | yes | - | One IP, or a CIDR range not wider than /8 (IPv4) or /16 (IPv6). |
| `reason` | recommended | 500 | Why it is trusted. |
| `added_by` | recommended | 100 | Who added it. |

```bash
curl --cacert "$CA" -X POST "$FURNACE/api/v1/whitelist/ips" \
  -H "Authorization: Bearer $WL_TOKEN" -H "Content-Type: application/json" \
  -d '{"entry":"198.51.100.0/24","reason":"Partner network","added_by":"admin.one"}'
```

`201`:

```json
{"status": "added", "message": "...", "cidr": "198.51.100.0/24", "removed_from_blocklist": ["198.51.100.7"]}
```

`200` `already_exists` when the same range is already there (`cidr` is included).

### 9.2 List

`GET /api/v1/whitelist/ips?limit=1000&offset=0`

```bash
curl --cacert "$CA" "$FURNACE/api/v1/whitelist/ips?limit=1000&offset=0" -H "Authorization: Bearer $WL_TOKEN"
```

`200`:

```json
{"status": "ok", "message": "1 entries on the IP whitelist.",
 "total": 1, "limit": 1000, "offset": 0,
 "entries": [{"cidr": "198.51.100.0/24", "added_at": "2026-10-26T09:20:00Z",
              "added_by": "admin.one", "reason": "Partner network"}]}
```

To get everything, call again with `offset` increased by `limit` until `offset >= total`.
`limit` or `offset` that is not a whole number gives `400`.

### 9.3 Remove

`DELETE /api/v1/whitelist/ips/<ip or cidr>` (optional `removed_by`). The slash in a range can stay as it is.

```bash
curl --cacert "$CA" -X DELETE "$FURNACE/api/v1/whitelist/ips/198.51.100.0/24" \
  -H "Authorization: Bearer $WL_TOKEN"
```

Answers: `200` `removed` (with `cidr`) or `404` `not_found`.
Removing a whitelist entry does not block anything again.

## 10. Whitelist: domains

Same as [section 9](#9-whitelist-ips-and-ranges), with the field `domain` and the path `/api/v1/whitelist/domains`.

```bash
curl --cacert "$CA" -X POST "$FURNACE/api/v1/whitelist/domains" \
  -H "Authorization: Bearer $WL_TOKEN" -H "Content-Type: application/json" \
  -d '{"domain":"good.example.com","reason":"Company website","added_by":"admin.one"}'

curl --cacert "$CA" "$FURNACE/api/v1/whitelist/domains?limit=1000&offset=0" \
  -H "Authorization: Bearer $WL_TOKEN"

curl --cacert "$CA" -X DELETE "$FURNACE/api/v1/whitelist/domains/good.example.com" \
  -H "Authorization: Bearer $WL_TOKEN"
```

Adding a domain removes it from the blocklist if it was blocked (`removed_from_blocklist`).

## 11. Bulk whitelist upload

`POST /api/v1/whitelist/ips/bulk` and `POST /api/v1/whitelist/domains/bulk`

Up to 200,000 entries per request. The whole upload runs in one transaction. Bad entries are skipped and listed.
Good entries are added. Use `dry_run` first to see what would happen. A dry run changes nothing and also works on
the follower.

**JSON body:**

| Field | Required | What to send |
|---|---|---|
| `entries` | yes | A list of IPs and ranges (or domains). |
| `dry_run` | no | `true` or `false` (default `false`). Must be a JSON boolean. |
| `reason` | no | Stored on every new entry. |
| `added_by` | no | Stored on every new entry. |

```bash
curl --cacert "$CA" -X POST "$FURNACE/api/v1/whitelist/ips/bulk" \
  -H "Authorization: Bearer $WL_TOKEN" -H "Content-Type: application/json" \
  -d '{"entries":["198.51.100.0/24","203.0.113.0/25","192.0.2.7"],"dry_run":true,
       "reason":"Partner ranges","added_by":"admin.one"}'
```

**Plain text body:** `Content-Type: text/plain`, one entry per line. Empty lines and lines starting with `#` are
skipped. `dry_run` (`true`, `1` or `yes`), `reason` and `added_by` go in the query string.

```bash
curl --cacert "$CA" -X POST "$FURNACE/api/v1/whitelist/ips/bulk?dry_run=true&added_by=admin.one" \
  -H "Authorization: Bearer $WL_TOKEN" -H "Content-Type: text/plain" --data-binary @whitelist.txt
```

`200`:

```json
{"status": "ok", "message": "2 entries would be added. 1 entries would be rejected. 1 blocked IPs would be removed.",
 "dry_run": true, "received": 3, "added": 2, "already_exists": 0,
 "rejected": [{"value": "10.0.0.0/4", "reason_code": "range_too_wide", "message": "..."}],
 "removed_from_blocklist": ["203.0.113.45"]}
```

## 12. List files for the list reader

The list reader (for example a firewall) downloads two plain text files. Each has one value per line.

| Path | Content |
|---|---|
| `GET /edl/blocklist.txt` | Active blocked IPs |
| `GET /edl/domains.txt` | Blocked domains |

- Login: HTTP basic auth. Setup makes the user `lists`. Manage logins with `sudo furnace edl-user list|add NAME|rotate NAME|remove NAME`.
- Rate limit: 2 requests per second per address (burst 10).
- If `LISTS_ALLOW_FROM` is set, other addresses get `403`.
- The answer has `Cache-Control: no-store`. The files change within a moment of each API change.

```bash
curl --cacert "$CA" -u lists:PASSWORD "$FURNACE/edl/blocklist.txt"
```

The archive CSV and the database are never served.

## 13. Health check

`GET /health`. No token. nginx allows it only from the servers themselves (localhost and the two server IPs).
It is for keepalived and the admins.

`200`:

```json
{"status": "ok", "version": "0.6.0", "role": "leader", "ips": 1234, "domains": 56}
```

`503` `error` when the database or the list files cannot be read.

## 14. How a caller should handle answers

| Answer | What to do |
|---|---|
| `201` | Tell the person "blocked" (or "whitelisted"). |
| `200` `already_exists` | Tell the person "already blocked since <date>" (`entry.added_at`). |
| `200` `removed` | Tell the person "removed". |
| `404` `not_found` | Tell the person it was not in the list. |
| `422` `rejected` | Show `message`. Do not retry. The input must change. |
| `503`, `429` page, network error | Retry a few times with a pause. If it still fails, tell the person. |
| `401` | The caller's token is wrong. Alert its admin. Do not retry: wrong tokens lead to a lockout. |
| `429` `locked` | Fix the token. Then ask the Furnace admin for `sudo furnace unlock <caller IP>`. |
| `500` | Tell the Furnace admin. The details are in the server log. |
