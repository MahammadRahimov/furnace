# Deployment guide 2: Docker, keepalived inside the container

Each of the two hosts runs one Furnace container. The container has everything inside: the API, nginx,
**keepalived**, replication, the daily backup and the nightly cleanup. This is the default.

[Back to the overview](README.md)

```
        list reader and caller ──► shared IP 192.0.2.10 (moves to the host that is alive)
                                     │
      host A 192.0.2.11              │              host B 192.0.2.12
      container "furnace"   ─── HTTPS every 20 s ──► container "furnace"
      (the leader)          ◄── VRRP (keepalived) ─► (the follower)
      ~/furnace/config  ~/furnace/data              ~/furnace/config  ~/furnace/data
```

## Before you start

- Two Linux hosts with Docker and the Compose plugin (`docker compose version` works). 1 to 2 GB memory each.
- Three IPs: host A `192.0.2.11` (leader), host B `192.0.2.12` (follower), shared IP `192.0.2.10` (examples).
- **No other keepalived on these hosts.** If the hosts already run keepalived for other services, use
  [guide 3](3-docker-host-keepalived.md).
- Port 443 free on both hosts.
- **The container never changes the host firewall.** Open these yourself (network and host firewall):

| From | To | Port |
|---|---|---|
| the list reader and the caller | the shared IP | TCP 443 |
| each host | the other host | TCP 443 |
| each host | the other host | IP protocol 112 (VRRP) |

  For example with ufw: `sudo ufw allow 443/tcp` and `sudo ufw allow from 192.0.2.12 to any proto vrrp` (on
  host A; the other IP on host B; a very old ufw without `vrrp` needs a raw rule for protocol 112 in
  `/etc/ufw/before.rules`). With firewalld:
  `sudo firewall-cmd --permanent --add-service=https` and
  `sudo firewall-cmd --permanent --add-rich-rule='rule family=ipv4 source address=192.0.2.12 protocol value=vrrp accept'`,
  then `sudo firewall-cmd --reload`.

The container uses the host network (`network_mode: host`) and two extra rights for keepalived, `NET_ADMIN` and
`NET_RAW`. It is not privileged.

## Step 1. Download and load (both hosts)

```bash
mkdir ~/furnace && cd ~/furnace
R=https://github.com/MahammadRahimov/furnace/releases/download/v0.10.0
curl -fLO $R/furnace-image-0.10.0.tar.gz -fLO $R/docker-compose.yml -fLO $R/env.example
docker load -i furnace-image-0.10.0.tar.gz        # "Loaded image: furnace:0.10.0"
mv env.example .env
```

`docker-compose.yml` is the same on both hosts: never edit it. Your settings go into `.env`.

## Step 2. Write `.env` (the same file on both hosts)

```bash
nano .env
```

```
FURNACE_LEADER_IP=192.0.2.11      # host A
FURNACE_FOLLOWER_IP=192.0.2.12    # host B
FURNACE_SHARED_IP=192.0.2.10      # the shared IP
```

Leave the rest as it is, or see the settings table at the end. Copy this file **as it is** to host B later:
nothing is swapped. Each host finds its role from its own IP.

## Step 3. Start the leader (host A)

```bash
cd ~/furnace
docker compose run --rm furnace check     # "Settings are fine: this host is the leader (192.0.2.11 ...)"
docker compose up -d
docker logs furnace                       # "Ready."
```

At the first start the leader makes the certificate, the keepalived password and the replication secret. They
stay in `~/furnace/config` (private: never share this folder).

## Step 4. Make the first API token and list login (host A)

```bash
docker exec -it furnace furnace token add soar         # asks the permission grid and the days
docker exec -it furnace furnace edl-user add paloalto  # the list login for the firewall
```

Each secret is shown **once**. Store it now.

## Step 5. Make a join code (host A)

```bash
docker exec furnace furnace join-code
```

It shows `FJ1-....` and the line to add. The code works **once**, for **1 hour**, only from host B.

## Step 6. Start the follower (host B)

Do step 1 on host B, and copy the **same `.env`** from host A. Then add the join code:

```bash
cd ~/furnace
echo 'FURNACE_JOIN_CODE=FJ1-ABCD-EFGH-IJKL-MNOP-QRST-UVWX-YZ23-4567' >> .env
docker compose run --rm furnace check     # "this host is the follower (192.0.2.12 ...)"
docker compose up -d
docker logs furnace                       # "fetched from the leader 192.0.2.11 with the join code" and "Ready."
```

The follower checks the leader's certificate against the code, then fetches the secrets over HTTPS. After that you
can remove the `FURNACE_JOIN_CODE` line (the code is used up).

**Join code not possible** (host B cannot reach port 443 on host A yet)? Copy the bundle file instead:

```bash
# host A
docker cp furnace:/etc/furnace/furnace-peer-bundle.tar.gz ~/ && chmod 600 ~/furnace-peer-bundle.tar.gz
# host B
mkdir -p ~/furnace/config
scp you@192.0.2.11:furnace-peer-bundle.tar.gz ~/furnace/config/
cd ~/furnace && docker compose up -d
# host A again
rm ~/furnace-peer-bundle.tar.gz
```

## Step 7. Check

```bash
docker exec furnace furnace status        # on both: role, processes, replication
```

From another machine:

```bash
curl -k -u paloalto:PASSWORD https://192.0.2.10/edl/blocklist.txt
```

**Failover test:** `docker stop furnace` on the leader. Within a few seconds host B owns the shared IP and is the
leader. `docker start furnace` brings host A back as the standby (it does not take the shared IP back).

## Step 8. Connect the list reader and the caller

The list reader uses `https://192.0.2.10/edl/blocklist.txt` (also `domains.txt`, `urls.txt`) with the list login.
The caller uses `https://192.0.2.10/api/v1/...` with the token. Both should trust the certificate:
`docker exec furnace cat /etc/furnace/tls/furnace.crt`.
Details: README [section 5](../../README.md#5-checklist-of-manual-work) and [section 6](../../README.md#6-api-guide).

## Settings (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `FURNACE_LEADER_IP` | (required) | host A's IP (the same on both hosts) |
| `FURNACE_FOLLOWER_IP` | (required) | host B's IP (the same on both hosts) |
| `FURNACE_SHARED_IP` | (required) | the shared IP |
| `FURNACE_SHARED_IP_PREFIX` | `24` | network prefix of the shared IP |
| `FURNACE_JOIN_CODE` | (none) | follower only, once |
| `FURNACE_INTERFACE` | the card that has this host's IP | the network card |
| `FURNACE_API_ALLOW_FROM` | `any` | who may call the API (a token is always needed) |
| `FURNACE_LISTS_ALLOW_FROM` | `any` | who may download the lists (the login is always needed) |
| `FURNACE_VRRP_ROUTER_ID` | `51` | the same on both hosts, unused by others on the network |
| `FURNACE_ARCHIVE_AFTER_DAYS` | `90` | days a temporary IP stays in the list |
| `FURNACE_MAX_IPS`, `FURNACE_MAX_DOMAINS`, `FURNACE_MAX_URLS` | `50000` | most entries per list |
| `TZ` | `UTC` | time zone of the backup and the cleanup |

Keep the settings the same on both hosts. After a change: `docker compose up -d`.

## Daily work, backup, upgrade

- Daily work: `docker exec furnace furnace status`, `docker exec -it furnace furnace token add NAME`,
  `docker logs --since 1h furnace`.
- Backup: the leader makes a daily backup in `~/furnace/data/backups`. Copy `~/furnace/config` and
  `~/furnace/data` to another place from time to time.
- Upgrade: `docker load -i` the new image, put the new `docker-compose.yml` in the folder (`.env` stays), then
  `docker compose up -d`. The leader first, then the follower. An old `.env` with `FURNACE_THIS_IP` /
  `FURNACE_PEER_IP` keeps working; the log suggests the new names.
