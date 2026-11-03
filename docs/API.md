# Furnace API reference

This is the full reference for the Furnace HTTP API and the three list files.
The README has the same content in a shorter form ([README section 6](../README.md#6-api-guide)).

## Contents

1. [Basics](#1-basics)
2. [Authentication and tokens](#2-authentication-and-tokens)
3. [Limits](#3-limits)
4. [Answers and status codes](#4-answers-and-status-codes)
5. [Reason codes](#5-reason-codes)
6. [Endpoint summary](#6-endpoint-summary)
7. [Blocked IPs](#7-blocked-ips) (and [the archive](#74-the-archive))
8. [Blocked domains](#8-blocked-domains)
8a. [Blocked URLs](#8a-blocked-urls)
9. [Whitelist: IPs and ranges](#9-whitelist-ips-and-ranges)
10. [Whitelist: domains](#10-whitelist-domains)
11. [Bulk whitelist upload](#11-bulk-whitelist-upload) (and [blocked IPs, domains and URLs from a file](#111-bulk-upload-of-blocked-ips-domains-and-urls-a-file))
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
TOKEN='paste-a-token-with-blocklist-write-here'
WL_TOKEN='paste-a-token-with-whitelist-write-here'
CA=furnace.crt                       # the Furnace certificate file
```

## 2. Authentication and tokens

Send the token in a header on every `/api/` call:

```
Authorization: Bearer <token>
```

**Permissions.** Each token has its own permissions, a grid of four boxes:

|  | Read | Write |
|---|---|---|
| **Blocklist** | `GET /api/v1/ips/<ip>`, `GET /api/v1/domains/<domain>` | `POST` and `DELETE` on `/api/v1/ips...` and `/api/v1/domains...`, bulk upload of a file (also `dry_run`) |
| **Whitelist** | `GET /api/v1/whitelist/ips`, `GET /api/v1/whitelist/domains` | `POST` and `DELETE` on `/api/v1/whitelist/...`, bulk upload (also `dry_run`) |

Write includes read. The permission names are `blocklist:read`, `blocklist:write`, `whitelist:read` and
`whitelist:write`.

**Expiry.** Each token has its own expiry, chosen when the token is made: 0 to 3650 days, 0 = never.

Setup makes one token and asks for its name, its boxes and its expiry. Manage tokens on the leader:

```bash
sudo furnace token list                    # permissions, expiry, last use; never the tokens
sudo furnace token add NAME                # asks the grid and the days; the token is shown once
sudo furnace token add NAME --allow blocklist:write,whitelist:read --expires-days 365
sudo furnace token edit NAME               # change the boxes or the expiry; the value stays
sudo furnace token rotate NAME             # new value for the same number of days; the old one stops at once
sudo furnace token remove NAME
```

Tokens are stored as SHA-256 hashes and are copied to the follower. Give every program its own token, with only
the boxes it needs.

**Refusals with a real token** (both `401`, neither counts towards the lockout):

```json
{"status": "unauthorized", "message": "This token may not do this. It needs the whitelist write permission."}
{"status": "unauthorized", "message": "This token expired on 2027-10-28. Ask the Furnace admin for a new one."}
```

A token that expires within 14 days, or has expired, is shown with `WARNING` in `sudo furnace status` and in the
nightly cleanup log.

**Lockout.** After 10 wrong tokens in 15 minutes, the calling address is locked for 15 minutes.
While it is locked, every call gets `429` `locked`, even with a good token. The `Retry-After` header gives the
seconds to wait. A missing token does not count as wrong. Unlock early with `sudo furnace unlock <caller IP>`.

**Address rules (optional).** By default any address may call the API. If `API_ALLOW_FROM` is set, other
addresses get a `403` page from nginx (not JSON).

## 3. Limits

| Limit | Value | What happens above it |
|---|---|---|
| API rate | 10 requests per second per address, burst 50 | `429` page from nginx |
| List file rate | 2 requests per second per address, burst 10 | `429` page from nginx |
| Request body | 20 MB | `413` `too_large` |
| Bulk entries | 200,000 per request | `400` `bad_request` |
| `added_by`, `removed_by` | 100 characters | `422` `field_too_long` |
| `reason` | 500 characters | `422` `field_too_long` |
| Whitelist list page | `limit` 1 to 10,000 (default 1,000) | Clamped to the range |
| Blocked IPs | `MAX_IPS` setting (the list reader's limit) | `422` `limit_reached` |
| Blocked domains, URLs | `MAX_DOMAINS`, `MAX_URLS` (setup sets both to the same limit) | `422` `limit_reached` |
| URL length | 255 characters (after cleaning) | `422` `url_too_long` |

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
| 401 | `unauthorized` | The token is missing or wrong, has expired, or lacks the permission. `message` says which. |
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
| `missing_value` | all adds | No IP, domain or URL was sent. |
| `invalid_ip` | IPs | Not a valid IP address. |
| `range_not_allowed` | IPs | A range (CIDR or a-b) was sent. Send one IP. |
| `private_ip`, `loopback_ip`, `link_local_ip`, `shared_address_space`, `multicast_ip`, `reserved_ip`, `unspecified_ip`, `not_public`, `ipv4_mapped_not_allowed` | IPs | Not a single public IP. |
| `documentation_range` | IPs | 192.0.2.0/24, 198.51.100.0/24 or 203.0.113.0/24, and test ranges are switched off. |
| `whitelisted` | IPs, domains, URLs | The value is on the whitelist, or a parent domain of it is. The message names the whitelist entry. |
| `limit_reached` | IPs, domains, URLs | The list is full (`MAX_IPS`, `MAX_DOMAINS`, `MAX_URLS`). |
| `invalid_domain` | domains | Not a valid domain name. |
| `wildcard_not_allowed` | domains | A wildcard like `*.example.com` was sent. |
| `url_not_allowed` | domains | A URL was sent. Send only the domain. |
| `ip_not_allowed` | domains | An IP was sent to a domain endpoint. |
| `invalid_url` | URLs | Not a valid URL: bad host name, port, spaces, or a non-ASCII path (send it percent-encoded). |
| `scheme_not_allowed` | URLs | Only `http://` and `https://` (they are removed). `ftp://` and others are refused. |
| `login_not_allowed` | URLs | The URL has `user@` or `user:password@`. |
| `url_too_long` | URLs | Longer than 255 characters. |
| `wildcard_partial` | URLs | A wildcard is only part of a name or path part (`*bad.example.com`). |
| `wildcard_too_wide` | URLs | The wildcard covers too much (`*.com/`, `example.*/`, `*.co.uk/`, `*.github.io/`). |
| `ipv6_url_not_allowed` | URLs | The host is an IPv6 address. Block it in the IP list. |
| `private_ip` and the other IP codes | URLs | The host is an IP that may not be blocked. |
| `whitelist_not_allowed` | whitelist domains | A shared platform (`github.io`), a public name (`co.uk`), a name that covers one (`amazonaws.com`), or a name of the local refuse file. The message says which. |
| `invalid_cidr` | whitelist IPs | Not a valid IP or range. |
| `range_too_wide` | whitelist IPs | Shorter than /8 (IPv4) or /16 (IPv6). |
| `field_too_long` | all | `reason`, `added_by` or `removed_by` is too long, or not text. |
| `invalid_permanent` | IPs | `permanent` is not `true` or `false`. |
| `not_leader` | all changes | This server is the follower (HTTP 503). |

## 6. Endpoint summary

| Method | Path | Permission | What it does |
|---|---|---|---|
| POST | `/api/v1/ips` | blocklist write | Block an IP |
| GET | `/api/v1/ips/<ip>` | blocklist read | Look up an IP |
| DELETE | `/api/v1/ips/<ip>` | blocklist write | Unblock an IP |
| POST | `/api/v1/ips/bulk` | blocklist write | Block the IPs of a file (like `furnace import-ips`) |
| POST | `/api/v1/domains` | blocklist write | Block a domain |
| GET | `/api/v1/domains/<domain>` | blocklist read | Look up a domain |
| DELETE | `/api/v1/domains/<domain>` | blocklist write | Unblock a domain |
| POST | `/api/v1/domains/bulk` | blocklist write | Block the domains of a file |
| POST | `/api/v1/urls` | blocklist write | Block a URL |
| GET | `/api/v1/urls?url=...` | blocklist read | Look up a URL |
| DELETE | `/api/v1/urls?url=...` | blocklist write | Unblock a URL |
| POST | `/api/v1/urls/bulk` | blocklist write | Block the URLs of a file (like `furnace import-urls`) |
| POST | `/api/v1/whitelist/ips` | whitelist write | Whitelist an IP or range |
| GET | `/api/v1/whitelist/ips` | whitelist read | List whitelisted IPs and ranges (paged) |
| DELETE | `/api/v1/whitelist/ips/<ip or cidr>` | whitelist write | Remove from the whitelist |
| POST | `/api/v1/whitelist/ips/bulk` | whitelist write | Bulk whitelist IPs and ranges |
| POST | `/api/v1/whitelist/domains` | whitelist write | Whitelist a domain |
| GET | `/api/v1/whitelist/domains` | whitelist read | List whitelisted domains (paged) |
| DELETE | `/api/v1/whitelist/domains/<domain>` | whitelist write | Remove from the whitelist |
| POST | `/api/v1/whitelist/domains/bulk` | whitelist write | Bulk whitelist domains |
| GET | `/edl/blocklist.txt` | list login | Blocked IPs, one per line |
| GET | `/edl/domains.txt` | list login | Blocked domains, one per line |
| GET | `/edl/urls.txt` | list login | Blocked URLs, one per line |
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
| `permanent` | no | - | `true`: the IP never expires. `false` or missing: temporary, it leaves the list after `ARCHIVE_AFTER_DAYS`. |
| `incident_id` | **no longer allowed** | - | Removed in 0.8. A request that still sends it gets `400` `bad_request`. Put an incident number into `reason` if you need it. |

```bash
curl --cacert "$CA" -X POST "$FURNACE/api/v1/ips" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"ip":"203.0.113.45","reason":"Phishing C2 server","added_by":"analyst.one"}'
```

A permanent block:

```bash
curl --cacert "$CA" -X POST "$FURNACE/api/v1/ips" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"ip":"203.0.113.46","reason":"Known C2 server","added_by":"analyst.one","permanent":true}'
```

`201` (new):

```json
{"status": "added", "message": "The IP is now blocked for 90 days.",
 "entry": {"ip": "203.0.113.45", "added_at": "2026-10-26T09:15:02Z", "added_by": "analyst.one",
           "reason": "Phishing C2 server",
           "permanent": false, "expires_at": "2027-01-24T09:15:02Z"},
 "record": {"first_blocked_at": "2026-10-26T09:15:02Z", "last_blocked_at": "2026-10-26T09:15:02Z",
            "times_blocked": 1, "added_by": "analyst.one", "reason": "Phishing C2 server"}}
```

- `entry.permanent` is `true` or `false`. `entry.expires_at` is the expiry of a temporary IP, `null` when permanent.
  The real removal happens in the nightly cleanup after that moment.
- `record` is the archive record ([section 7.4](#74-the-archive)).
- If the IP was blocked before (it is in the archive), it is blocked again with a new date,
  `"restored_from_archive": true`, and `record.times_blocked` goes up by one.

`200` (already blocked; nothing changes, also not `permanent`, and the old date is kept):

```json
{"status": "already_exists",
 "message": "This IP is already blocked since 2026-10-20T08:00:00Z. Nothing changed.",
 "entry": {"ip": "203.0.113.45", "added_at": "2026-10-20T08:00:00Z", "...": "..."}}
```

Other answers: `422` `rejected` ([section 5](#5-reason-codes), for example `invalid_permanent`), `503` `not_leader`
or `busy`.

Temporary IPs leave the list after `ARCHIVE_AFTER_DAYS` (nightly cleanup). Permanent IPs stay until an unblock.
Both stay in the archive.

### 7.2 Look up an IP

`GET /api/v1/ips/<ip>`

```bash
curl --cacert "$CA" "$FURNACE/api/v1/ips/203.0.113.45" -H "Authorization: Bearer $TOKEN"
```

`200`:

```json
{"status": "found", "message": "This IP is blocked.", "list": "active",
 "entry": {"ip": "203.0.113.45", "added_at": "2026-10-26T09:15:02Z", "added_by": "analyst.one",
           "reason": "Phishing C2 server",
           "permanent": false, "expires_at": "2027-01-24T09:15:02Z"},
 "record": {"first_blocked_at": "2026-10-26T09:15:02Z", "last_blocked_at": "2026-10-26T09:15:02Z",
            "times_blocked": 1, "added_by": "analyst.one", "reason": "Phishing C2 server"},
 "whitelisted_by": null}
```

An IP that was blocked before, but not now:

```json
{"status": "found", "message": "This IP is in the archive: it was blocked before. It is not blocked now.",
 "list": "archive",
 "entry": {"ip": "203.0.113.45", "first_blocked_at": "2026-10-26T09:15:02Z", "last_blocked_at": "...",
           "times_blocked": 1, "added_by": "analyst.one", "reason": "Phishing C2 server"},
 "record": {"...": "..."}, "whitelisted_by": null}
```

- `list` is `active` (blocked now) or `archive` (blocked before, not now: expired, unblocked or whitelisted).
  For `archive`, `entry` is the archive record with the IP.
- `record` is the archive record ([section 7.4](#74-the-archive)).
- `404` `not_found` means it was never blocked.
- `whitelisted_by` is the whitelist range that covers the IP, or `null`.
- `whitelisted_by` is still included with `404`.

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

The IP leaves the list file. It stays in the archive.

### 7.4 The archive

The archive is a record of every different IP, domain and URL that was ever blocked:

- One row per IP, domain or URL. Never a duplicate, never removed.
- Written when it is blocked for the first time, at the same time as the list.
- It keeps the details of the first block (`added_by`, `reason`), `first_blocked_at`,
  `last_blocked_at` and `times_blocked`.
- Expiry, unblock and the whitelist remove an entry only from the list, never from the archive.
- Blocked again later: back in the list with a new date, `last_blocked_at` is new and `times_blocked` goes up by one.

Fields of `record`:

| Field | What |
|---|---|
| `first_blocked_at` | When it was blocked for the first time. |
| `last_blocked_at` | When it was blocked the last time. |
| `times_blocked` | How many times it went into the list. |
| `added_by`, `reason` | The details of the first block. |

The archive is not served as a file. The admin gets it with `sudo furnace export archive`.

## 8. Blocked domains

Same as IPs, with the field `domain`. Domains are stored in lower case. They never expire, so there is no
`permanent` field. Domains get the archive record too: a look-up answers `list` `active` or `archive`, with `record`.
Only exact domains are accepted: no wildcards, no URLs, no IPs.

### 8.1 Block a domain

`POST /api/v1/domains`

Body: `domain` (required), `reason`, `added_by` (same limits as [7.1](#71-block-an-ip)).

```bash
curl --cacert "$CA" -X POST "$FURNACE/api/v1/domains" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"domain":"bad.example.com","reason":"Malware download site","added_by":"analyst.one"}'
```

Answers: `201` `added`, `200` `already_exists`, `422` `rejected`, `503`.

### 8.2 Look up a domain

`GET /api/v1/domains/<domain>`

```bash
curl --cacert "$CA" "$FURNACE/api/v1/domains/bad.example.com" -H "Authorization: Bearer $TOKEN"
```

Answers: `200` `found` with `entry`, or `404` `not_found`. Both have `whitelisted` (`true` or `false`) and
`whitelisted_by`: the whitelist domain that protects it (the domain itself or a parent of it), or `null`.

### 8.3 Unblock a domain

`DELETE /api/v1/domains/<domain>` (optional `removed_by`)

```bash
curl --cacert "$CA" -X DELETE "$FURNACE/api/v1/domains/bad.example.com?removed_by=analyst.one" \
  -H "Authorization: Bearer $TOKEN"
```

Answers: `200` `removed` or `404` `not_found`.

## 8a. Blocked URLs

The URL list is for a firewall's URL list (on Palo Alto: an EDL of type URL List, used as a custom URL category).
It blocks the web traffic itself, also when the client already knows the IP. URLs never expire, like domains, and
get the archive record. The permission is the blocklist permission, the same as for IPs and domains.

A URL has slashes, so the look-up and the unblock take it in the query string (`?url=`, URL-encoded) or in a JSON
body (`{"url": "..."}`), never in the path.

**How Furnace cleans a URL** (the answer shows the stored form in `entry.url`; `given` shows what was sent when it
differs):

| Rule | Sent | Stored and published |
|---|---|---|
| `http://` and `https://` are removed (one entry covers both) | `https://bad.example.com/login` | `bad.example.com/login` |
| Lower case (the firewall ignores case) | `Bad.Example.COM/Login` | `bad.example.com/login` |
| A host alone gets a `/` (so it does not also match `bad.example.com.evil.test`) | `bad.example.com` | `bad.example.com/` |
| The `#...` part is removed (a browser never sends it) | `bad.example.com/a#top` | `bad.example.com/a` |
| A query without a path gets the `/` | `bad.example.com?id=1` | `bad.example.com/?id=1` |
| A port stays | `bad.example.com:8080` | `bad.example.com:8080/` |
| Non-ASCII names become punycode | `bücher.example` | `xn--bcher-kva.example/` |

**Wildcards** are whole parts only. Parts are separated by `. / ? & = ; +`. `*` is one or more parts, `^` exactly one.

| Entry | Matches | Refused because |
|---|---|---|
| `*.bad.example.com/` | `www.bad.example.com`, `a.b.bad.example.com` (not `bad.example.com` itself) | |
| `^.bad.example.com/` | `www.bad.example.com` (one part only) | |
| `bad.example.com/*/login` | `bad.example.com/x/login` | |
| `*bad.example.com/` | | `wildcard_partial` |
| `*.com/`, `example.*/`, `bad.*.com/` | | `wildcard_too_wide`: the last two parts must be written out |
| `*.co.uk/`, `*.com.az/` | | `wildcard_too_wide`: under a country's public names, three parts |
| `*.github.io/`, `*.blogspot.com/`, `*.amazonaws.com/` | | `wildcard_too_wide`: a shared platform or public name (Public Suffix List), or a name that covers one |

To block a site and all its subdomains, add both `bad.example.com/` and `*.bad.example.com/`.

**The whitelist protects by host** (the domain whitelist and the IP whitelist; there is no separate URL whitelist).
A domain on the whitelist protects itself and all its subdomains:

| Whitelist | URL | Result |
|---|---|---|
| `good.example.com` | `good.example.com/login`, `www.good.example.com/`, `*.good.example.com/` | refused (`whitelisted`) |
| `good.example.com` | `*.example.com/`, `^.example.com/` (they cover `good.example.com`) | refused (`whitelisted`) |
| `good.example.com` | `example.com/`, `bad.example.com/` | allowed |
| `198.51.100.0/24` | `198.51.100.7/x` | refused (`whitelisted`) |

Adding a whitelist entry removes the blocked URLs it protects (`urls_removed_from_blocklist` in the answer).

**Paths and HTTPS:** for HTTPS the firewall sees the path only with SSL decryption. Without it, only host entries
(`bad.example.com/`, `*.bad.example.com/`) work for HTTPS sites. Paths always work for plain HTTP.

### 8a.1 Block a URL

`POST /api/v1/urls`. Body: `url` (required), `reason`, `added_by` (same limits as [7.1](#71-block-an-ip)).

```bash
curl --cacert "$CA" -X POST "$FURNACE/api/v1/urls" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"url":"https://bad.example.com/login/","reason":"Phishing page","added_by":"analyst.one"}'
```

`201`:

```json
{"status": "added", "message": "The URL is now blocked as bad.example.com/login/.",
 "entry": {"url": "bad.example.com/login/", "added_at": "2026-11-02T15:03:27Z", "added_by": "analyst.one",
           "reason": "Phishing page"},
 "record": {"first_blocked_at": "2026-11-02T15:03:27Z", "last_blocked_at": "2026-11-02T15:03:27Z",
            "times_blocked": 1, "added_by": "analyst.one", "reason": "Phishing page"},
 "given": "https://bad.example.com/login/"}
```

Other answers: `200` `already_exists` (also when it was sent in another form, for example with `http://`),
`422` `rejected`, `503`.

### 8a.2 Look up a URL

`GET /api/v1/urls?url=...`. Send the URL in any form; it is cleaned the same way first.

```bash
curl --cacert "$CA" -G "$FURNACE/api/v1/urls" --data-urlencode "url=https://bad.example.com/login/" \
  -H "Authorization: Bearer $TOKEN"
```

Answers: `200` `found` with `list` (`active` or `archive`), `entry`, `record` and `whitelisted_by` (the whitelist
domain or range that protects the URL, or `null`); `404` `not_found` (with `url` and `whitelisted_by`); `400` when
no URL was given.

### 8a.3 Unblock a URL

`DELETE /api/v1/urls?url=...` (optional `removed_by`), or `DELETE /api/v1/urls` with a JSON body
`{"url": "...", "removed_by": "..."}`.

```bash
curl --cacert "$CA" -X DELETE -G "$FURNACE/api/v1/urls" --data-urlencode "url=bad.example.com/login/" \
  --data-urlencode "removed_by=analyst.one" -H "Authorization: Bearer $TOKEN"
```

Answers: `200` `removed` or `404` `not_found`. The URL stays in the archive.

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

Adding a domain removes it **and its subdomains** from the domain blocklist (`removed_from_blocklist`), and the
blocked URLs it protects (`urls_removed_from_blocklist`). Blocking the domain or a subdomain of it later is refused
(`whitelisted`, the message names the whitelist entry).

**What the domain whitelist does and what it refuses** (user decisions 2026-11-02):

- A domain on the whitelist protects **itself and all its subdomains**, in the domain list and in the URL list.
  With `x.example.com` on the whitelist: the domains `x.example.com`, `mail.x.example.com` and `a.b.x.example.com`
  and the URLs `x.example.com/...`, `www.x.example.com/...` and `*.x.example.com/` cannot be blocked, and adding the
  whitelist entry removes the ones already blocked. The parent `example.com` and look-alikes such as
  `notx.example.com` or `x.example.com.evil.test` are not touched.
- **Shared platforms and public names cannot be whitelisted** (`whitelist_not_allowed`). Anyone can make a site
  under them, so whitelisting one would protect attackers' sites too. Furnace uses the **Public Suffix List**
  (publicsuffix.org, the list browsers use; a copy comes with each Furnace release, no internet needed):

  | Whitelist request | Result |
  |---|---|
  | `github.io`, `blogspot.com`, `cloudfront.net`, `azurewebsites.net`, `vercel.app`, `herokuapp.com`, `pages.dev` | refused: a shared platform |
  | `co.uk`, `com.az`, `gov.az` | refused: a public name |
  | `amazonaws.com`, `windows.net` | refused: they cover a shared platform (`s3.amazonaws.com`, `blob.core.windows.net`) |
  | `company.github.io`, `example.co.uk`, `yourcompany.com`, `google.com`, `microsoft.com`, `github.com` | allowed |

- **Your own names** go into `/etc/furnace/whitelist-refuse.txt` (one per line, `#` comments; setup makes it
  once, empty, and never changes it). A name there is refused, and so is every name that would cover it:
  `drive.google.com` in the file also refuses `google.com`. Use it for services that host anyone's files under a
  path, which the Public Suffix List cannot know (`dropbox.com`, `drive.google.com`). Keep the file the same on both
  servers.
- Whitelist entries made before 0.8 stay. `furnace status` warns about the ones that would be refused today.

A refused whitelist request answers `422`:

```json
{"status": "rejected", "reason_code": "whitelist_not_allowed",
 "message": "github.io is a shared platform: anyone can make a site under it. Whitelist the exact site instead, for example company.github.io."}
```

In a bulk upload, refused names are listed in `rejected` with the same code; the other entries are added.

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
{"status": "ok", "message": "2 entries would be added. 1 entries would be rejected. 1 blocked IPs and 0 blocked URLs would be removed.",
 "dry_run": true, "received": 3, "added": 2, "already_exists": 0,
 "rejected": [{"value": "10.0.0.0/4", "reason_code": "range_too_wide", "message": "..."}],
 "removed_from_blocklist": ["203.0.113.45"], "urls_removed_from_blocklist": []}
```

### 11.1 Bulk upload of blocked IPs, domains and URLs (a file)

`POST /api/v1/ips/bulk`, `POST /api/v1/domains/bulk` and `POST /api/v1/urls/bulk` (permission: blocklist write)

Send a whole file of IPs or domains in one request. It uses exactly the same rules as `sudo furnace import-ips` and
`import-domains` on the server ([README section 7.2](../README.md#72-import-ips)):

- Lines: `ip`, `ip,date` or `ip,date,reason` (domains: `domain` or `domain,reason`). Comma, semicolon, tab or
  spaces. A header line, blank lines and comments (`#`, `;`, `//`) are skipped.
- IPs with a date keep it and expire `ARCHIVE_AFTER_DAYS` after it. A date older than that goes only into the
  archive (or with `old_ips=fresh` into the list with a new date). IPs without a date get spread-out expiry dates.
  A date more than a day in the future is skipped (`future_date`).
- Whitelisted, invalid, private, duplicate and already active lines are skipped and listed with a reason code.
- All or nothing: one transaction, a database backup first. If the list would grow over `MAX_IPS` (or
  `MAX_DOMAINS`), the upload is refused (`422`) and nothing changes.
- Up to 200,000 lines and 20 MB per request. Only the leader takes uploads (the follower answers `503`
  `not_leader`, also for a dry run). A report of the skipped lines stays on the server in
  `/var/lib/furnace/reports/`.

**Plain text body** (the file as it is). Options in the query string:

| Option | Default | What it does |
|---|---|---|
| `dry_run` | `false` | `true`: check and answer, change nothing |
| `permanent` | `false` | IPs only. `true`: every IP of the file is permanent (never expires) |
| `old_ips` | `archive` | IPs only. `fresh`: old dates go into the list with a new date |
| `date_format` | found automatically | IPs only. For example `%d/%m/%Y` |
| `reason` | `uploaded through the API` | For lines without a reason |
| `added_by` | the token name | Stored on every new entry |

```bash
curl --cacert "$CA" -X POST "$FURNACE/api/v1/ips/bulk?dry_run=true&added_by=admin.one" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: text/plain" --data-binary @blocklist.csv
```

**JSON body:** the same options as fields, and `entries`: a list of lines (strings).

```bash
curl --cacert "$CA" -X POST "$FURNACE/api/v1/domains/bulk" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"entries":["bad.example.com","worse.example.net,malware"],"reason":"feed"}'
```

`200`:

```json
{"status": "ok", "message": "DRY RUN - nothing was changed. Read 4 lines. Added to the active list: 2. ...",
 "dry_run": true, "received": 4, "added": 2, "permanent": false, "archived": 1, "blocked_again": 0,
 "skipped": 1, "skipped_lines": [{"line": 1, "reason_code": "header", "text": "ip,date,reason"}],
 "report": "api-ips-k2j3h4.txt.20261101-101500.report.txt"}
```

`skipped_lines` lists at most 1,000 lines; `skipped` is the full number. Domains and URLs have no `permanent` and
`archived` (they never expire).

**URL files** (`/api/v1/urls/bulk`, the same as `sudo furnace import-urls`): one URL per line, optionally followed
by a space or a tab and a reason. A URL can have commas and semicolons, so they do not split the line:

```
url
https://bad.example.com/login phishing page
*.evil.example.org	malware
bad.example.net/a,b
```

Each URL is cleaned as in [section 8a](#8a-blocked-urls). The same URL in another form (`http://bad.example.com/x`
and `bad.example.com/x`) counts as a duplicate. Options: `dry_run`, `reason`, `added_by`. The limit is `MAX_URLS`.

## 12. List files for the list reader

The list reader (for example a firewall) downloads plain text files. Each has one value per line.

| Path | Content |
|---|---|
| `GET /edl/blocklist.txt` | Active blocked IPs |
| `GET /edl/domains.txt` | Blocked domains |
| `GET /edl/urls.txt` | Blocked URLs (for a URL list / custom URL category) |

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
{"status": "ok", "version": "0.8.0", "role": "leader", "ips": 1234, "domains": 56, "urls": 7}
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
| `401` | The token is wrong, expired or lacks a permission (see `message`). Alert its admin. Do not retry: wrong tokens lead to a lockout. |
| `429` `locked` | Fix the token. Then ask the Furnace admin for `sudo furnace unlock <caller IP>`. |
| `500` | Tell the Furnace admin. The details are in the server log. |
